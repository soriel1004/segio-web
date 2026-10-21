#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SEGIO 4.2 approval archive. Python 2.6+ and PostgreSQL 8.4+; stdlib only.

Run on the server as an authorized administrator. No DB or auth changes.
The selected employee ID scopes an administrator export; it is not a login.
"""
from __future__ import print_function

import base64
import binascii
import csv
import datetime
import errno
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from optparse import OptionParser
try:
    from HTMLParser import HTMLParser
    from urlparse import urlsplit, parse_qs
    from urllib import unquote, unquote_plus
    unquote_to_bytes = unquote
    html_unescape = HTMLParser().unescape
except ImportError:
    from html.parser import HTMLParser
    from html import unescape as html_unescape
    from urllib.parse import urlsplit, parse_qs, unquote, unquote_plus, unquote_to_bytes

from export_schema_from_segio_config import (
    SafeError, read_config, discover_config, connection_settings, pgpass_escape)

PY2 = sys.version_info[0] == 2
HERE = os.path.dirname(os.path.abspath(__file__))
NULL = "__SEGIO_NULL__"
EXPORTER_REVISION = 'db-native-paths-1'


def text(value):
    if value is None:
        return u""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if PY2:
        return unicode(value)
    return str(value)


def escape(value):
    return text(value).replace(u"&", u"&amp;").replace(u"<", u"&lt;").replace(u">", u"&gt;").replace(u'"', u"&quot;").replace(u"'", u"&#39;")


def decode_display_markup(value):
    """Unwrap confirmed document encodings; leave ordinary HTML/text intact.

    Operates on a display copy only. Stop as soon as real markup is reached,
    so literal entities, plus signs and percent escapes inside it survive.
    """
    value = text(value)
    transforms = []
    for unused in range(6):
        leading = value.lstrip()
        if re.match(r"^%(?:3c|26(?:lt|amp)|25(?:3c|26|25))", leading, re.I):
            value = (unquote_plus(value.encode("utf-8")).decode("utf-8") if PY2 else
                     unquote_plus(value, encoding="utf-8", errors="strict"))
            transforms.append("urlencoded_html")
        elif re.match(r"^&(?:amp;)*lt;\s*(?:!doctype|html|head|body|p|div|span|table|thead|tbody|tr|td|th|br|hr|h[1-6]|ul|ol|li|font|center|b|strong|i|em|u|pre|blockquote|img|a)\b", leading, re.I):
            value = html_unescape(value)
            transforms.append("escaped_html")
        else:
            break
    return value, transforms


def signature_data_uri(value):
    encoded = re.sub(r"\s+", "", text(value))
    if not encoded or len(encoded) > 28 * 1024 * 1024 or not re.match(r"^[A-Za-z0-9+/]+={0,2}$", encoded):
        return None
    try:
        data = base64.b64decode(encoded.encode("ascii") + b"=" * ((-len(encoded)) % 4))
    except (ValueError, TypeError, binascii.Error):
        return None
    mime = raster_mime(data)
    if not mime or len(data) > 20 * 1024 * 1024:
        return None
    return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")


def tokens(value):
    # Source: edit_format.php_ and edit_format.html use comma-separated
    # UID@@name (reference), UID@@group@@name (auto_pass); scalar/CSV receivers.
    return [part.split("@@", 1)[0].strip() for part in text(value).split(",") if part.strip()]


def member_sql(column, uid, structured=False):
    condition = "strpos(tok.value,'@@')>0 AND " if structured else ""
    return ("EXISTS (SELECT 1 FROM regexp_split_to_table(COALESCE(%s,''), ',') "
            "AS tok(value) WHERE %sbtrim(split_part(tok.value,'@@',1))=%s)" % (column, condition, uid))


def selection_sql(username, live_scope=False):
    uid = "convert_from(decode('%s','hex'),'UTF8')" % binascii.hexlify(username.encode("utf-8")).decode("ascii")
    choices = []
    for table in ("approval_document", "approval_document_backup"):
        rules = ["d.uid=" + uid]
        for field in ("reference", "auto_pass", "doc_recv_uid"):
            rules.append(member_sql("d." + field, uid, structured=field != "doc_recv_uid"))
        for suffix in ("", "_backup"):
            rules.append("EXISTS (SELECT 1 FROM public.approval_line%s l WHERE l.p_no=d.no AND (l.uid=%s OR l.target_id=%s))" % (suffix, uid, uid))
            send_rule = " OR ".join(["s.doc_uid=" + uid, member_sql("s.recv_user", uid), member_sql("s.recv_uid", uid)])
            rules.append("EXISTS (SELECT 1 FROM public.approval_send_box%s s WHERE s.doc_no=d.no AND (%s))" % (suffix, send_rule))
            if not live_scope:
                rules.append("EXISTS (SELECT 1 FROM public.approval_comment_info%s c WHERE c.doc_no=d.no::text AND c.uid=%s)" % (suffix, uid))
                rules.append("EXISTS (SELECT 1 FROM public.approval_doc_backup%s b WHERE b.p_no=d.no AND b.uid=%s)" % (suffix, uid))
        if not live_scope:
            rules.append("EXISTS (SELECT 1 FROM public.approval_favorite f WHERE f.doc_no=d.no AND f.uid=%s)" % uid)
        choices.append("SELECT d.no FROM public.%s d WHERE (%s)" % (table, " OR ".join(rules)))
    return " UNION ".join(choices), uid


def make_queries(username, model, live_scope=False):
    ids, uid = selection_sql(username, live_scope)
    queries = {}
    for table in model:
        if table in ("approval_document", "approval_document_backup"):
            condition = "t.no IN (%s)" % ids
        elif table in ("approval_line", "approval_line_backup", "approval_doc_backup", "approval_doc_backup_backup"):
            condition = "t.p_no IN (%s)" % ids
        elif table in ("approval_attach_info", "approval_comment_info", "approval_comment_info_backup"):
            condition = "t.doc_no IN (SELECT no::text FROM (%s) selected)" % ids
        elif table in ("approval_send_box", "approval_send_box_backup", "approval_favorite"):
            condition = "t.doc_no IN (%s)" % ids
        elif table == "approval_format_info":
            condition = ("t.no::text IN (SELECT fno FROM public.approval_document WHERE no IN (%s) "
                         "UNION SELECT fno FROM public.approval_document_backup WHERE no IN (%s))" % (ids, ids))
        elif table == "approval_member_info":
            condition = ("t.uid=%s OR t.uid IN (SELECT uid FROM public.approval_document WHERE no IN (%s) "
                         "UNION SELECT uid FROM public.approval_document_backup WHERE no IN (%s) "
                         "UNION SELECT uid FROM public.approval_line WHERE p_no IN (%s) "
                         "UNION SELECT uid FROM public.approval_line_backup WHERE p_no IN (%s) "
                         "UNION SELECT target_id FROM public.approval_line WHERE p_no IN (%s) "
                         "UNION SELECT target_id FROM public.approval_line_backup WHERE p_no IN (%s))" % (uid, ids, ids, ids, ids, ids, ids))
        else:
            raise SafeError("Unsupported model table; no query executed.")
        if table == "approval_member_info":
            # Comment authors may not be on a document's approval line. Preserve
            # their display names too, before account deletion changes lookup.
            for suffix in ("", "_backup"):
                condition += (" OR t.uid IN (SELECT uid FROM public.approval_comment_info%s "
                              "WHERE doc_no IN (SELECT no::text FROM (%s) selected))" % (suffix, ids))
                condition += (" OR t.uid IN (SELECT uid FROM public.approval_doc_backup%s "
                              "WHERE p_no IN (%s))" % (suffix, ids))
        columns = [quote_identifier(c) for c in model[table]]
        queries[table] = ("SELECT %s FROM public.%s t WHERE %s ORDER BY t.%s" %
                          (",".join(columns), quote_identifier(table), condition,
                           quote_identifier("id" if "id" in model[table] else "no")))
    return queries


def quote_identifier(value):
    if not re.match(r"^[a-z_][a-z_0-9]*$", value):
        raise SafeError("Invalid model identifier.")
    return '"' + value + '"'


def copy_command(query, name, columns):
    # Hex text preserves NULL vs empty text and avoids CSV multiline ambiguity
    # without requiring PostgreSQL's newer JSON functions.
    fields = ["encode(convert_to(q.%s::text,'UTF8'),'hex') AS %s" %
              (quote_identifier(c), quote_identifier(c)) for c in columns]
    return ("\\copy (SELECT %s FROM (%s) q) TO '%s.csv' WITH CSV HEADER NULL '%s'" %
            (",".join(fields), query, name, NULL))


def read_rows(path):
    csv.field_size_limit(256 * 1024 * 1024)
    result = []
    stream = open(path, "rb") if PY2 else io.open(path, "r", encoding="ascii", newline="")
    try:
        for row in csv.DictReader(stream):
            if None in row or None in row.values():
                raise SafeError("Malformed export CSV; archive not completed.")
            decoded = {}
            for key, value in row.items():
                decoded[text(key)] = None if text(value) == NULL else binascii.unhexlify(value).decode("utf-8")
            result.append(decoded)
    finally:
        stream.close()
    return result


def database_error_code(error):
    # Verbose PostgreSQL errors expose a SQLSTATE. Never return raw stderr,
    # which can contain credentials, SQL values, paths or application data.
    states = re.findall(b"(?:ERROR|FATAL):[ \\t]+([0-9A-Z]{5}):", error)
    codes = {b"28P01": "db_authentication_failed", b"28000": "db_authentication_failed",
             b"42501": "db_permission_denied", b"42P01": "db_schema_mismatch",
             b"42703": "db_schema_mismatch", b"42883": "db_schema_mismatch",
             b"57014": "db_query_timeout", b"3D000": "db_database_missing",
             b"53100": "server_disk_full"}
    for state in states:
        if state in codes:
            return codes[state]
        if state.startswith(b"08"):
            return "db_connection_failed"
    # Connection failures can be produced by libpq before a SQLSTATE exists.
    for token, code in ((b"authentication failed", "db_authentication_failed"),
                        (b"no password supplied", "db_authentication_failed"),
                        (b"permission denied", "db_permission_denied"),
                        (b"could not connect", "db_connection_failed"),
                        (b"connection to server", "db_connection_failed"),
                        (b"statement timeout", "db_query_timeout")):
        if token in error.lower():
            return code
    return "db_query_failed"


def psql_readonly(config, work, sql, binary="/usr/bin/psql"):
    try:
        host, user, database, password = connection_settings(read_config(config))
    except SafeError:
        raise SafeError("Existing DB configuration is unsupported; values omitted.", "db_config_invalid")
    except (IOError, OSError):
        raise SafeError("Existing DB configuration cannot be read.", "db_config_unreadable")
    passfile = os.path.join(work, "pgpass")
    try:
        descriptor = os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            for passhost in ([host, "localhost"] if host.startswith("/") else [host]):
                fields = [passhost, "5432", database, user, password]
                stream.write(b":".join([pgpass_escape(v) for v in fields]) + b"\n")
        env = dict(os.environ)
        for key in list(env):
            if key.startswith("PG"):
                del env[key]
        env.update({"PGPASSFILE": passfile, "PGCONNECT_TIMEOUT": "5", "LC_ALL": "C",
                    "PGOPTIONS": "-c default_transaction_read_only=on -c statement_timeout=60000"})
        command = [binary, "-X", "-q", "-w", "-h", host, "-p", "5432", "-U", user,
                   "-d", database, "-v", "ON_ERROR_STOP=1"]
        try:
            process = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise SafeError("The configured psql client could not be started.", "psql_unavailable")
        output, error = process.communicate(("\n".join(sql) + "\n").encode("ascii"))
        if process.returncode or b"SEGIO_DB_EXPORT_COMPLETE" not in output:
            code = database_error_code(error)
            raise SafeError("%s (psql exit %d); raw errors omitted." % (code, process.returncode), code)
        return output
    finally:
        if os.path.exists(passfile):
            os.unlink(passfile)


def query_database(config, username, work, binary="/usr/bin/psql", live_scope=False):
    with open(os.path.join(HERE, "approval_db_columns.json"), "rb") as stream:
        model = json.loads(stream.read().decode("utf-8"))
    queries = make_queries(username, model, live_scope)
    sql = ["\\set ON_ERROR_STOP on", "\\set VERBOSITY verbose",
           "BEGIN ISOLATION LEVEL SERIALIZABLE READ ONLY;",
           "SET LOCAL statement_timeout=60000;", "SET LOCAL client_encoding='UTF8';"]
    counts = " UNION ALL ".join(["SELECT '%s'::text AS table_name,count(*)::text AS row_count FROM (%s) counted" % (table, queries[table]) for table in sorted(queries)])
    sql.append(copy_command(counts, "counts", ["table_name", "row_count"]))
    for table in sorted(queries):
        sql.append(copy_command(queries[table], table, model[table]))
    sql.extend(["ROLLBACK;", "\\echo SEGIO_DB_EXPORT_COMPLETE"])
    psql_readonly(config, work, sql, binary)
    expected = dict((r["table_name"], int(r["row_count"])) for r in read_rows(os.path.join(work, "counts.csv")))
    rows = dict((table, read_rows(os.path.join(work, table + ".csv"))) for table in model)
    if set(expected) != set(rows) or any(len(rows[t]) != expected[t] for t in rows):
        raise SafeError("Snapshot row count mismatch; archive not completed.", "db_snapshot_mismatch")
    return rows, expected


def raster_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class FrozenHTML(HTMLParser):
    tags = set("p div span br hr table thead tbody tfoot tr td th ul ol li b strong i em u s strike sub sup pre blockquote h1 h2 h3 h4 h5 h6 font center img a".split())
    blocked = set("script style iframe object embed svg math video audio noscript template".split())
    attributes = set("colspan rowspan width height align valign border cellpadding cellspacing alt title color size face".split())
    css = set("text-align text-indent font-size font-weight font-style font-family color background-color border border-top border-bottom border-left border-right border-collapse width height max-width padding padding-left padding-right margin margin-left margin-right vertical-align white-space line-height".split())

    def __init__(self, owner):
        HTMLParser.__init__(self)
        self.owner, self.parts, self.plain, self.suppressed = owner, [], [], []

    def handle_starttag(self, tag, attrs):
        if self.suppressed:
            if tag in self.blocked:
                self.suppressed.append(tag)
            return
        if tag in self.blocked:
            self.suppressed.append(tag)
            return
        if tag not in self.tags:
            return
        attributes = []
        if tag == "img":
            src = self.owner.image(dict(attrs).get("src", ""))
            if not src:
                self.parts.append(u"[이미지 미확보]")
                return
            attributes.append('src="' + escape(src) + '"')
        for key, value in attrs:
            if key in self.attributes:
                attributes.append(key + '="' + escape(value) + '"')
            if key == "style":
                rules = []
                for rule in text(value).split(";"):
                    name, separator, setting = rule.partition(":")
                    if separator and name.strip().lower() in self.css and not re.search(r"url|expression|javascript|@|\\|[<>]", setting, re.I):
                        rules.append(name.strip().lower() + ":" + setting)
                attributes.append('style="' + escape(";".join(rules)) + '"')
        self.parts.append("<" + tag + (" " + " ".join(attributes) if attributes else "") + ">")

    def handle_endtag(self, tag):
        if self.suppressed:
            if tag == self.suppressed[-1]:
                self.suppressed.pop()
            return
        if tag in self.tags and tag not in ("img", "br", "hr"):
            self.parts.append("</" + tag + ">")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(escape(data))
            self.plain.append(text(data))

    def handle_entityref(self, name):
        if not self.suppressed:
            self.parts.append("&" + name + ";")
            self.plain.append("&" + name + ";")

    def handle_charref(self, name):
        if not self.suppressed:
            self.parts.append("&#" + name + ";")
            self.plain.append("&#" + name + ";")


class Bundle:
    def __init__(self, folder, cell, username):
        self.folder, self.cell, self.username = folder, os.path.realpath(cell), username
        self.files, self.issues, self.image_cache, self.attachments_by_no = {}, [], {}, {}
        self.profiles = {}
        self.display_decoding = {"urlencoded_html": 0, "escaped_html": 0, "base64_signature": 0}

    def issue(self, kind, value):
        entry = {"kind": kind, "reference": text(value)}
        if entry not in self.issues:
            self.issues.append(entry)

    def write(self, path, data):
        target = os.path.join(self.folder, path)
        parent = os.path.dirname(target)
        if not os.path.isdir(parent):
            os.makedirs(parent, 0o700)
        if not isinstance(data, bytes):
            data = data.encode("utf-8")
        with open(target, "wb") as stream:
            stream.write(data)
        self.files[path] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        return path

    def local_file(self, reference, upload=False):
        # DB text is Unicode, but legacy POSIX files may have UTF-8 or CP949
        # byte names while Python 2.6 runs under an ASCII daemon locale.
        # Never let Python implicitly encode a Unicode filename using ASCII.
        try:
            reference = text(reference)
        except UnicodeDecodeError:
            try:
                reference = reference.decode('cp949')
            except UnicodeError:
                return None
        parsed = urlsplit(reference)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return None
        try:
            encoded = unquote_to_bytes(parsed.path.encode('utf-8') if PY2 else parsed.path)
            try:
                value = encoded.decode('utf-8')
            except UnicodeDecodeError:
                value = encoded.decode('cp949')
        except UnicodeDecodeError:
            return None
        if value.startswith("/data/"):
            value = value[1:]
        if not (value.startswith("data/") or os.path.isabs(value) or
                (upload and "/" not in value and "\\" not in value)):
            return None
        matches = {}
        codecs = (None,) if os.name == 'nt' else (sys.getfilesystemencoding() or 'utf-8','utf-8','cp949')
        for codec in codecs:
            try:
                cell = self.cell
                native = value
                data, uploads, separator = 'data', 'data/approval/upload', os.sep
                if codec is not None:
                    cell = cell if isinstance(cell,bytes) else cell.encode(codec)
                    native = value.encode(codec)
                    data, uploads, separator = data.encode('ascii'), uploads.encode('ascii'), os.sep.encode('ascii')
                if value.startswith('data/'):
                    path = os.path.join(cell,native)
                elif os.path.isabs(value):
                    path = native
                else:
                    path = os.path.join(cell,uploads,native)
                allowed = os.path.realpath(os.path.join(cell,uploads if upload else data))
                resolved = os.path.realpath(path)
                if resolved.startswith(allowed+separator) and os.path.isfile(resolved):
                    info = os.stat(resolved)
                    matches[(info.st_dev,info.st_ino)] = resolved
            except (UnicodeError, OSError, IOError):
                continue
        # Two different files for the same displayed name are ambiguous.
        return next(iter(matches.values())) if len(matches)==1 else None

    def image(self, src):
        src = text(src)
        if src in self.image_cache:
            return self.image_cache[src]
        result = None
        if src.startswith("data:"):
            try:
                header, encoded = src.split(",", 1)
                if re.match(r"^data:image/(png|jpeg|gif|webp);base64$", header, re.I) and len(encoded) <= 28 * 1024 * 1024 and re.match(r"^[A-Za-z0-9+/=\s]+$", encoded):
                    data = base64.b64decode(encoded)
                    mime = raster_mime(data)
                    if mime:
                        result = "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")
            except (ValueError, TypeError, binascii.Error):
                pass
        else:
            path = self.local_file(src)
            parsed = urlsplit(src)
            if not path and not parsed.scheme and not parsed.netloc and parsed.path.split("/")[-1] == "img_insert.php":
                args = parse_qs(parsed.query)
                if args.get("mode") == ["attach_img"] and len(args.get("fno", [])) == 1:
                    row = self.attachments_by_no.get(args["fno"][0])
                    path = self.local_file(row["fpath"], upload=True) if row else None
                if args.get("mode") == ["app_images"] and len(args.get("fname", [])) == 1:
                    filename = args["fname"][0]
                    if filename not in (".", "..") and "/" not in filename and "\\" not in filename:
                        path = self.local_file("data/approval/app_images/" + filename)
                if len(args.get("id", [])) == 1:
                    profile = self.profiles.get(args["id"][0], {})
                    path = self.local_file(profile.get("sign_path") or "")
            if path:
                try:
                    before = os.stat(path)
                    with open(path, "rb") as stream:
                        data = stream.read(20 * 1024 * 1024 + 1)
                    after = os.stat(path)
                    mime = raster_mime(data)
                    if mime and len(data) <= 20 * 1024 * 1024 and self.same_file(before, after):
                        result = "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")
                except (OSError, IOError):
                    pass
        if not result:
            self.issue("image_unresolved", src)
        self.image_cache[src] = result
        return result

    @staticmethod
    def same_file(before, after):
        return (before.st_ino, before.st_size, before.st_mtime, before.st_ctime) == (after.st_ino, after.st_size, after.st_mtime, after.st_ctime)

    def attachment(self, row, document):
        path = self.local_file(row["fpath"], upload=True)
        if not path:
            self.issue("attachment_missing_or_unsafe", row["no"])
            return None
        extension = os.path.splitext(text(row["fname"]))[1]
        if not re.match(r"^\.[A-Za-z0-9]{1,10}$", extension):
            extension = ".bin"
        relative = "documents/%s/attachments/%s%s" % (document, row["no"], extension)
        target = os.path.join(self.folder, relative)
        parent = os.path.dirname(target)
        if not os.path.isdir(parent):
            os.makedirs(parent, 0o700)
        try:
            before = os.stat(path)
            digest, size = hashlib.sha256(), 0
            with open(path, "rb") as source:
                with open(target, "wb") as destination:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                        size += len(block)
                        destination.write(block)
            after = os.stat(path)
            if not stat.S_ISREG(before.st_mode) or not self.same_file(before, after) or size != before.st_size:
                raise IOError("File changed while copying")
            self.files[relative] = {"bytes": size, "sha256": digest.hexdigest()}
            return {"name": row["fname"], "path": relative, "bytes": size}
        except (OSError, IOError):
            if os.path.exists(target):
                os.unlink(target)
            self.issue("attachment_copy_failed_or_changed", row["no"])
            return None

    def freeze(self, value, signature=False):
        original = text(value)
        try:
            display, transforms = decode_display_markup(original)
        except UnicodeDecodeError:
            self.issue("display_markup_encoding_unresolved", "stored_value")
            display, transforms = original, []
        for transform in transforms:
            self.display_decoding[transform] += 1
        if signature:
            image = signature_data_uri(display)
            if image:
                self.display_decoding["base64_signature"] += 1
                return u'<img alt="저장된 서명" src="' + escape(image) + '">', u""
        parser = FrozenHTML(self)
        try:
            parser.feed(display)
            parser.close()
            return u"".join(parser.parts), u" ".join(parser.plain)
        except Exception:
            self.issue("html_parse_failed", "body")
            return "<pre>" + escape(display) + "</pre>", display


def group_rows(rows, field):
    result = {}
    for row in rows:
        result.setdefault(text(row[field]), []).append(row)
    return result


def when(value):
    try:
        if value and int(value) > 0:
            return (datetime.datetime.utcfromtimestamp(int(value)) + datetime.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S KST")
    except (ValueError, OverflowError, OSError):
        pass
    return text(value)


def render_bundle(bundle, rows, counts, allow_empty=False):
    members = dict((r["uid"], r) for r in rows["approval_member_info"])
    bundle.profiles = members
    if not allow_empty and not rows["approval_document"] and not rows["approval_document_backup"] and bundle.username not in members:
        raise SafeError("Target user and related documents not found; archive not completed.", "approval_user_not_found")
    grouped = {}
    for table in rows:
        field = "p_no" if table in ("approval_line", "approval_line_backup", "approval_doc_backup", "approval_doc_backup_backup") else "doc_no"
        if rows[table] and field in rows[table][0]:
            grouped[table] = group_rows(rows[table], field)
    documents = list(rows["approval_document"])
    backups = group_rows(rows["approval_document_backup"], "no")
    existing = set(d["no"] for d in documents)
    # Each historical version gets its own page, even if no active row remains.
    versions = [(d, "active", d["no"]) for d in documents]
    for no in sorted(backups):
        for document in backups[no]:
            versions.append((document, "history", no + "-history-" + document["id"]))
    bundle.attachments_by_no = dict((r["no"], r) for r in rows["approval_attach_info"])
    formats = dict((r["no"], r) for r in rows["approval_format_info"])
    index = []
    attachment_cache = {}
    for document, source, identity in versions:
        no = document["no"]
        if not re.match(r"^\d+(?:-history-\d+)?$", identity):
            raise SafeError("Unexpected document key; archive not completed.")
        related = dict((table, records.get(no, [])) for table, records in grouped.items())
        for records in related.values():
            for record in records:
                if record.get("doc_no") is not None and text(record["doc_no"]) != no:
                    raise SafeError("Related document mismatch.")
        if no not in attachment_cache:
            attachment_cache[no] = [a for a in [bundle.attachment(r, no) for r in related.get("approval_attach_info", [])] if a]
        attachments = attachment_cache[no]
        raw = {"source": source, "document": document, "related": related,
               "format": formats.get(document.get("fno")),
               "value_format": "Database text values; JSON null preserves SQL NULL"}
        raw_path = "documents/%s/database.json" % identity
        bundle.write(raw_path, json.dumps(raw, ensure_ascii=False, indent=2))
        context, search = bundle.freeze(document.get("context"))
        profile = members.get(document.get("uid"), {})
        lines = related.get("approval_line" if source == "active" else "approval_line_backup", [])
        lines = sorted(lines, key=lambda r: (int(r.get("order_no") or 0), int(r["no"])))
        author = next((r.get("rname") for r in lines if r.get("uid") == document.get("uid") and r.get("rname")), None) or profile.get("rname") or document.get("uid")
        if source == "history":
            # Keep all timestamped line rows below, without claiming a join
            # between independently archived versions that we cannot prove.
            author = (text(profile["rname"]) + u" [추출 시점 회원명]" if profile.get("rname") else document.get("uid"))
        line_html, line_text = [], []
        for line in lines:
            signature, signature_text = bundle.freeze(line.get("sign"), signature=True)
            values = [line.get(k) for k in ("order_no", "division", "rname", "uid", "class", "gname", "state", "target_id")]
            line_text.extend([text(v) for v in values])
            line_html.append("<tr>" + "".join("<td>" + escape(v) + "</td>" for v in values) + "<td>" + escape(when(line.get("wdate"))) + "</td><td>" + escape(when(line.get("backupdate"))) + "</td><td>" + signature + "</td></tr>")
        comments = related.get("approval_comment_info" if source == "active" else "approval_comment_info_backup", [])
        comment_html = "".join("<li><b>" + escape(members.get(r.get("uid"), {}).get("rname") or r.get("uid")) + " (" + escape(r.get("uid")) + ")</b> " + escape(r.get("wdate")) + (u" · 백업시각: " + escape(when(r.get("backupdate"))) if r.get("backupdate") else u"") + "<pre>" + escape(r.get("context")) + "</pre></li>" for r in comments)
        links = "".join('<li><a href="../../' + escape(a["path"]) + '">' + escape(a["name"]) + "</a></li>" for a in attachments)
        title = document.get("title") or u"제목 없음"
        page = u'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>%s</title><style>body{font:12px 'Malgun Gothic',sans-serif;margin:24px;overflow-wrap:anywhere}table{border-collapse:collapse;max-width:100%%}td,th{border:1px solid #bbb;padding:6px}img{max-width:100%%}pre{white-space:pre-wrap;font:inherit}.notice{background:#fff2cc;padding:12px} @media print{.navigation{display:none}}</style></head><body><p class="navigation"><a href="../../index.html">보관함 목록</a> · <a href="database.json">원본 DB 기록</a> · 브라우저 인쇄에서 PDF로 저장할 수 있습니다.</p><h1>%s</h1><p>문서번호: %s · 기안자: %s (%s) · 상태: %s · 자료: %s</p><p>기안: %s / 완료: %s</p><p class="notice">DB 자료로 재구성한 보관본입니다. 원래 인쇄 서식과 다를 수 있습니다. 이미지·첨부 미확보 및 수집 범위는 목록의 확인 사항을 보세요.</p><h2>저장된 결재선 기록</h2><p>과거 문서는 관련 결재선의 각 백업 시각을 함께 표시합니다. 문서 버전과 동일 시점인지 대조가 필요합니다.</p><table><tr><th>순서</th><th>구분</th><th>이름</th><th>ID</th><th>직급</th><th>부서</th><th>상태</th><th>대결 대상</th><th>일시</th><th>백업 기록 시각</th><th>서명</th></tr>%s</table><h2>본문</h2>%s<h2>의견</h2><ul>%s</ul><h2>첨부</h2><ul>%s</ul></body></html>''' % (escape(title), escape(title), escape(document.get("doc_id")), escape(author), escape(document.get("uid")), escape(document.get("progress")), escape(source), escape(when(document.get("wdate"))), escape(when(document.get("cdate"))), u"".join(line_html), context, comment_html, links)
        page_path = bundle.write("documents/%s/document.html" % identity, page)
        if not document.get("context"):
            bundle.issue("empty_document_body", identity)
        if source == "history":
            bundle.issue("historical_line_timestamps_require_review", identity)
        for ref in tokens(document.get("link")) + tokens(document.get("orig_doc_no")):
            if ref and ref not in existing and ref not in backups:
                bundle.issue("linked_document_outside_scope", ref)
        index.append({"id": identity, "title": title, "path": page_path, "json": raw_path,
                      "source": source, "attachments": attachments,
                      "search": u" ".join([search, text(author), text(document.get("doc_id")), text(document.get("uid"))] + line_text + [text(r.get("context")) for r in comments])})
    bundle.write("metadata/members.json", json.dumps(rows["approval_member_info"], ensure_ascii=False, indent=2))
    return index


def reserve_archive_path(output, username):
    number = 1
    while True:
        suffix = "" if number == 1 else "-%d" % number
        path = os.path.join(os.path.abspath(output), "segio-%s%s.zip" % (username, suffix))
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as error:
            if error.errno != errno.EEXIST:
                raise
            number += 1
            continue
        os.close(descriptor)
        return path


def run_export(config, username, output, cell=None, binary="/usr/bin/psql", progress=None, live_scope=False, pack=True):
    notify = progress or (lambda message: None)
    if not re.match(r"^[A-Za-z0-9_.@-]{1,100}$", username):
        raise SafeError("Use an exact employee ID containing letters, digits, _, ., @ or -.")
    cell = os.path.realpath(cell or os.path.join(os.path.dirname(config), os.pardir))
    if not os.path.isdir(os.path.join(cell, "data")):
        raise SafeError("Cell data directory not found. Specify --cell with the actual SEGIO cell path.", "cell_data_missing")
    old_umask = os.umask(0o077)
    folder = tempfile.mkdtemp(prefix="segio-db-" + username + "-", dir=os.path.abspath(output))
    work = os.path.join(folder, "private-query")
    os.mkdir(work, 0o700)
    archive_path = None
    success = False
    try:
        notify("Reading approval records in one read-only DB snapshot...")
        rows, counts = query_database(config, username, work, binary, live_scope)
        notify("Snapshot checked: %d active documents, %d historical versions." %
               (counts["approval_document"], counts["approval_document_backup"]))
        shutil.rmtree(work)
        bundle = Bundle(folder, cell, username)
        notify("Copying attachments and building offline HTML...")
        # The live bridge already authenticated the employee. An employee with
        # no approval profile/documents must still be able to archive mail.
        items = render_bundle(bundle, rows, counts, allow_empty=live_scope)
        bundle.write("catalog.json", json.dumps({"items": items}, ensure_ascii=False))
        warnings = [u"계정 삭제 영향은 검증되지 않았습니다.",
                    u"개인 ID로 확인된 기안·결재·대결·참조·자동회람·개인 수신·댓글·즐겨찾기 및 관련 이력을 수집했습니다. 부서 공용 수신함·기타 권한으로 열리는 문서는 범위가 확인되지 않았습니다.",
                    u"DB는 한 읽기 전용 스냅샷으로 조회했습니다. 서버 파일은 이후 복사하므로 DB와 파일의 동일 시점을 보장하지 않습니다.",
                    u"approval_ext_info/report/work 계열의 별도 업무·확장 기록과 메일은 이번 수집 범위에 포함하지 않았습니다.",
                    u"현재 첨부 목록을 사용합니다. 과거 버전 당시의 첨부와 동일한지는 확인되지 않았습니다.",
                    u"동적 이미지 주소·외부 이미지·사용자별 인쇄 서식은 원래 화면과 대조해야 합니다."]
        if bundle.issues:
            warnings.append(u"파일·본문·관련 문서 확인 사항: %d개 (issues.json 참조)" % len(bundle.issues))
        if live_scope:
            warnings[1] = u"로그인 ID의 기안·결재선·대결·참조·개인 수신 관계로 연결되는 DB 기록과 이력을 추출했습니다. 댓글·즐겨찾기만으로 연결된 문서는 제외합니다. 관리자 설치 시 승인한 개인 기록 보관 범위이며 SEGIO 화면별 권한과 동일하다고 검증되지 않았습니다."
            warnings[3] = u"approval_ext_info/report/work 계열의 별도 업무·확장 기록은 이번 DB 수집 범위에 포함하지 않았습니다. 메일 선택 결과는 manifest.json의 mail 항목을 확인하세요."
        if not items:
            warnings.append(u"개인 관계로 확인된 문서가 0개입니다. 실제 결재함과 대조하기 전에는 자료가 없다고 판단할 수 없습니다.")
        manifest = {"format": "segio-db-html/1", "server": "cell_000", "user": username,
                    "state": "collected_with_warnings", "account_deletion_verified": False,
                    "finished_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "counts": {"approval": len(items)}, "snapshot_rows": counts,
                    "coverage": [{"kind": "approval", "status": "personal_relations_only", "saved": len(items)}],
                    "warnings": warnings, "files": bundle.files,
                    "display_decoding": bundle.display_decoding}
        bundle.write("issues.json", json.dumps(bundle.issues, ensure_ascii=False, indent=2))
        payload = json.dumps({"items": items, "manifest": manifest}, ensure_ascii=False)
        payload = payload.replace(u"<", u"\\u003c").replace(u">", u"\\u003e").replace(u"&", u"\\u0026")
        with open(os.path.join(HERE, "db_archive_index.html"), "rb") as stream:
            template = stream.read().decode("utf-8")
        # Final manifest.json also includes the index and summary hashes.
        manifest["files"] = dict(bundle.files)
        bundle.write("index.html", template.replace("__DATA__", payload))
        summary = ("SEGIO_DB_EXPORT_V1\nActive documents: %d\nHistorical document versions: %d\n"
                   "Attachment records: %d\nFiles copied: %d\nIssues: %d\n"
                   "URL-decoded HTML layers: %d\nEntity-decoded HTML layers: %d\nBase64 signatures displayed: %d\n"
                   "Scope: personal relations only; mail and department-only access excluded\n"
                   "State: collected_with_warnings\nAccount deletion verified: false\n"
                   "END_SEGIO_DB_EXPORT_V1\n" %
                   (counts["approval_document"], counts["approval_document_backup"], counts["approval_attach_info"],
                    len([p for p in bundle.files if "/attachments/" in p]), len(bundle.issues),
                    bundle.display_decoding["urlencoded_html"], bundle.display_decoding["escaped_html"],
                    bundle.display_decoding["base64_signature"]))
        bundle.write("export-summary.txt", summary)
        manifest["files"] = dict(bundle.files)
        # The manifest cannot hash itself.
        with open(os.path.join(folder, "manifest.json"), "wb") as stream:
            stream.write(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        if not pack:
            success = True
            return folder, None
        notify("Creating ZIP archive...")
        archive_path = reserve_archive_path(output, username)
        archive = zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True)
        try:
            for root, dirs, files in os.walk(folder):
                for filename in sorted(files):
                    path = os.path.join(root, filename)
                    archive.write(path, os.path.relpath(path, folder).replace(os.sep, "/"))
        finally:
            archive.close()
        success = True
        return folder, archive_path
    finally:
        if not success:
            shutil.rmtree(folder)
            if archive_path and os.path.exists(archive_path):
                os.unlink(archive_path)
        os.umask(old_umask)


def main():
    parser = OptionParser(usage="%prog --user EMPLOYEE_ID [--config /cell/etc/db.conf] [--cell /cell]")
    parser.add_option("--user", dest="username", help="Exact employee ID, not the DB role")
    parser.add_option("--config", dest="config", help="Existing SEGIO db.conf path")
    parser.add_option("--cell", dest="cell", help="Actual SEGIO cell directory, if not beside etc/db.conf")
    parser.add_option("--output", dest="output", default=os.getcwd(), help="Existing output directory")
    options, args = parser.parse_args()
    if args or not options.username:
        parser.error("--user EMPLOYEE_ID is required. Do not pass a password.")
    try:
        config = options.config or discover_config()
        def progress(message):
            print(message)
            sys.stdout.flush()
        folder, archive = run_export(config, options.username, options.output, options.cell, progress=progress)
        print("Saved archive ZIP: " + archive)
        print("Saved summary: " + os.path.join(folder, "export-summary.txt"))
        print("Move ZIP to Windows and open index.html. Send export-summary.txt only.")
        return 0
    except SafeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (IOError, OSError, ValueError, binascii.Error):
        print("Export failed; incomplete output removed. Verify paths, permissions and disk space. "
              "Do not send db.conf or credentials.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
