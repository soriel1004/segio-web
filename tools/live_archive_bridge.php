<?php
/* SEGIO on-demand archive bridge, PHP 5.3 compatible; native identity only. */
ini_set('display_errors', '0');
header('Cache-Control: no-store, private');
header('X-Content-Type-Options: nosniff');
function answer($status, $value) {
    header('HTTP/1.1 '.$status, true, $status);
    header('Content-Type: application/json; charset=UTF-8');
    echo json_encode($value); exit;
}
function problem($status, $code) {
    global $root, $action, $runtime_failure;
    // Start can fail before a job exists. Keep an anonymous fixed error too.
    // Do not call save_object here: a storage failure must not recurse.
    if (isset($root,$action) && $action==='start') {
        $temporary=@tempnam($root.'/jobs','.failure-');
        if ($temporary!==false) {
            $value=array('protocol'=>'segio-live-archive/1','phase'=>$code,
                'diagnostic'=>array('stage'=>'server_request','category'=>'bridge','errno'=>null),
                'failed_at'=>gmdate('Y-m-d\TH:i:s\Z'));
            if (isset($runtime_failure)) $value['diagnostic']['runtime']=$runtime_failure;
            if (@file_put_contents($temporary,json_encode($value))!==false) {
                @chmod($temporary,0600);
                $target=$root.'/jobs/last-failure.json';
                if (DIRECTORY_SEPARATOR==='\\' && is_file($target)) @unlink($target);
                @rename($temporary,$target);
            }
            if (is_file($temporary)) @unlink($temporary);
        }
    }
    answer($status, array('protocol'=>'segio-live-archive/1','error'=>$code));
}
function public_worker_result($result) {
    $public=array();
    foreach(array('state','exit_code','output_bytes','stderr_present') as $name)
        if (isset($result[$name])) $public[$name]=$result[$name];
    return $public;
}
function run_fixed_worker($config, $root, $arguments, $require_token) {
    $result=array('state'=>'process_unavailable','exit_code'=>null,
                  'output_bytes'=>0,'stderr_present'=>false);
    foreach(array('proc_open','proc_close','proc_get_status','proc_terminate',
                  'stream_set_blocking','stream_get_contents','escapeshellarg','usleep') as $name)
        if (!function_exists($name)) return $result;
    $command=escapeshellarg($config['python']).' '.escapeshellarg($config['worker']);
    foreach($arguments as $argument) $command.=' '.escapeshellarg($argument);
    $pipes=array();
    $process=@proc_open($command,array(0=>array('pipe','r'),1=>array('pipe','w'),2=>array('pipe','w')),$pipes,$root);
    if (!is_resource($process)) { $result['state']='spawn_failed'; return $result; }
    fclose($pipes[0]);
    $stdout_ready=@stream_set_blocking($pipes[1],false);
    $stderr_ready=@stream_set_blocking($pipes[2],false);
    $nonblocking=$stdout_ready && $stderr_ready;
    // Windows PHP does not always support nonblocking anonymous pipes. Poll
    // the child first there; read bounded output only after it has exited.
    if (!$nonblocking && DIRECTORY_SEPARATOR!=='\\') {
        @proc_terminate($process); fclose($pipes[1]); fclose($pipes[2]); proc_close($process);
        $result['state']='pipe_setup_failed'; return $result;
    }
    $output=''; $exit=null; $finished=false; $deadline=microtime(true)+15.0;
    do {
        if ($nonblocking) {
            $chunk=stream_get_contents($pipes[1],65-strlen($output));
            if (is_string($chunk)) $output.=$chunk;
            $error=stream_get_contents($pipes[2],4096);
            if (is_string($error) && strlen($error)) $result['stderr_present']=true;
        }
        $status=proc_get_status($process);
        if (is_array($status) && !$status['running']) {
            // PHP <= 8.2 can consume the exit status here. Cache it before close.
            $exit=$status['exitcode']; $finished=true; break;
        }
        if (strlen($output)>=65 || microtime(true)>=$deadline) break;
        usleep(10000);
    } while (true);
    if (!$finished) @proc_terminate($process);
    if (($finished || $nonblocking) && strlen($output)<65) {
        $chunk=stream_get_contents($pipes[1],65-strlen($output));
        if (is_string($chunk)) $output.=$chunk;
    }
    if ($finished || $nonblocking) {
        $error=stream_get_contents($pipes[2],4096);
        if (is_string($error) && strlen($error)) $result['stderr_present']=true;
    }
    fclose($pipes[1]); fclose($pipes[2]);
    $closed=proc_close($process);
    if ($exit===null || $exit<0) $exit=$closed;
    $result['exit_code']=$exit;
    $result['output_bytes']=strlen($output);
    if (!$finished) $result['state']=strlen($output)>=65?'output_limit':'timeout';
    elseif ($exit===-1) $result['state']='exit_status_unavailable';
    elseif ($exit!==0) $result['state']='worker_exit_failed';
    elseif ($require_token && !preg_match('/\A[0-9a-f]{32}\r?\n?\z/',$output)) $result['state']='invalid_token';
    else {
        $result['state']='ok';
        if ($require_token) $result['token']=trim($output);
    }
    return $result;
}
function new_job_id($config, $root) {
    $bytes=false;
    if (function_exists('random_bytes')) {
        try { $bytes=random_bytes(16); } catch (Exception $error) { $bytes=false; }
    }
    if (!is_string($bytes) || strlen($bytes)!==16) {
        $random=@fopen('/dev/urandom','rb');
        $bytes=$random?fread($random,16):false;
        if ($random) fclose($random);
    }
    if ((!is_string($bytes) || strlen($bytes)!==16) && function_exists('openssl_random_pseudo_bytes')) {
        $strong=false;
        $bytes=openssl_random_pseudo_bytes(16,$strong);
        if (!$strong) $bytes=false;
    }
    if (is_string($bytes) && strlen($bytes)===16) return bin2hex($bytes);
    // Native SEGIO PHP may restrict /dev access and lack OpenSSL. Ask the
    // installed worker for OS entropy, using fixed arguments and no secrets.
    global $runtime_failure;
    $result=run_fixed_worker($config,$root,array('--random-token'),true);
    if ($result['state']!=='ok') {
        $runtime_failure=public_worker_result($result);
        problem(503,'secure_random_unavailable');
    }
    return $result['token'];
}
function object_file($path) {
    if (!is_file($path) || is_link($path) || filesize($path)>65536) return false;
    return json_decode(file_get_contents($path), true);
}
function save_object($path, $value) {
    $temporary = tempnam(dirname($path), '.update-');
    if ($temporary===false || file_put_contents($temporary,json_encode($value))===false)
        problem(503,'job_storage_failed');
    chmod($temporary,0600);
    if (DIRECTORY_SEPARATOR==='\\' && file_exists($path)) unlink($path);
    if (!rename($temporary,$path)) problem(503,'job_storage_failed');
}
function same_token($a,$b) {
    if (!is_string($a) || strlen($a)!==strlen($b)) return false;
    $difference=0; for($i=0;$i<strlen($a);$i++) $difference |= ord($a[$i]) ^ ord($b[$i]);
    return $difference===0;
}
function remove_job($path) {
    foreach(scandir($path) as $name) {
        if ($name==='.' || $name==='..') continue;
        $file=$path.'/'.$name;
        if (is_dir($file) && !is_link($file)) remove_job($file); else unlink($file);
    }
    rmdir($path);
}
if (in_array(strtolower((string)ini_get('register_globals')),array('1','on','yes','true'),true))
    problem(503,'insecure_php_configuration');
$a=isset($_ENV['REMOTE_USER'])?$_ENV['REMOTE_USER']:'';
$b=isset($_SERVER['REMOTE_USER'])?$_SERVER['REMOTE_USER']:'';
if ($a!=='' && $b!=='' && $a!==$b) problem(401,'identity_conflict');
$uid=$a!==''?$a:$b;
if (!is_string($uid) || !preg_match('/\A[A-Za-z0-9_.@-]{1,100}\z/',$uid) ||
    $uid==='.' || $uid==='..' || strtolower($uid)==='guest') problem(401,'native_login_required');
if (isset($_ENV['REMOTE_USERLEVEL']) && (string)$_ENV['REMOTE_USERLEVEL']==='0') problem(401,'native_login_required');
$root='__PRIVATE_ROOT__';
$config=object_file($root.'/config.json');
if (!$config || !isset($config['csrf_secret'],$config['worker'],$config['python'])) {
    // No paths, settings, secret values or application records in diagnostics.
    answer(503,array('protocol'=>'segio-live-archive/1','error'=>'service_not_configured',
        'checks'=>array('config_exists'=>@is_file($root.'/config.json'),
                        'config_readable'=>@is_readable($root.'/config.json'),
                        'config_json_object'=>is_array($config),
                        'php_path_restriction'=>(string)ini_get('open_basedir')!=='',
                        'php_safe_mode'=>in_array(strtolower((string)ini_get('safe_mode')),array('1','on','yes','true'),true))));
}
$method=isset($_SERVER['REQUEST_METHOD'])?$_SERVER['REQUEST_METHOD']:(isset($_ENV['REQUEST_METHOD'])?$_ENV['REQUEST_METHOD']:'');
$action=isset($_GET['action'])?$_GET['action']:'info';
if (!is_string($action) || !in_array($action,array('info','diagnostic','start','status','download','cleanup'),true)) problem(400,'unknown_action');
foreach($_GET as $key=>$value) if ($key!=='action' && $key!=='job') problem(400,'unexpected_parameter');
$post=($action==='start' || $action==='cleanup');
if (($post && $method!=='POST') || (!$post && $method!=='GET' && $method!=='HEAD')) problem(405,'read_only');
$hour=(int)floor(time()/3600);
$csrf=hash_hmac('sha256',$uid.'|'.$hour,$config['csrf_secret']);
if ($action==='diagnostic') {
    $probe=run_fixed_worker($config,$root,array('--random-token'),true);
    answer(200,array('protocol'=>'segio-live-archive/1','bridge_revision'=>'php-runtime-2',
        'checks'=>array('proc_open'=>function_exists('proc_open'),
            'proc_close'=>function_exists('proc_close'),
            'proc_get_status'=>function_exists('proc_get_status'),
            'proc_terminate'=>function_exists('proc_terminate'),
            'stream_set_blocking'=>function_exists('stream_set_blocking'),
            'stream_get_contents'=>function_exists('stream_get_contents'),
            'escapeshellarg'=>function_exists('escapeshellarg'),
            'php_path_restriction'=>(string)ini_get('open_basedir')!=='',
            'php_safe_mode'=>in_array(strtolower((string)ini_get('safe_mode')),array('1','on','yes','true'),true),
            'python_executable'=>@is_executable($config['python']),
            'worker_readable'=>@is_readable($config['worker'])),
        'worker'=>public_worker_result($probe)));
}
if ($action==='info') answer(200,array('protocol'=>'segio-live-archive/1','username'=>$uid,
    'csrf'=>$csrf,'capabilities'=>array('live_db'=>true,'mbox'=>!empty($config['mail_template']))));
if ($post) {
    foreach($_POST as $key=>$value) if (!in_array($key,array('csrf','approvals','mail'),true)) problem(400,'unexpected_parameter');
    $token=isset($_POST['csrf'])?$_POST['csrf']:'';
    if (!same_token($token,$csrf) && !same_token($token,hash_hmac('sha256',$uid.'|'.($hour-1),$config['csrf_secret'])))
        problem(403,'csrf_required');
}
$jobs=$root.'/jobs';
if ($action==='start') {
    $approvals=isset($_POST['approvals']) && $_POST['approvals']==='1';
    $mail=isset($_POST['mail']) && $_POST['mail']==='1';
    if (!$approvals && !$mail) problem(400,'empty_selection');
    if ($mail && empty($config['mail_template'])) problem(503,'mail_path_unconfigured');
    if (!function_exists('proc_open')) problem(503,'export_process_unavailable');
    $gate=fopen($jobs.'/.gate','a+');
    if (!$gate || !flock($gate,LOCK_EX)) problem(503,'job_storage_failed');
    $active=0;
    foreach(scandir($jobs) as $id) {
        if (!preg_match('/\A[a-f0-9]{32}\z/',$id) || is_link($jobs.'/'.$id)) continue;
        $request=object_file($jobs.'/'.$id.'/request.json');
        $status=object_file($jobs.'/'.$id.'/status.json');
        if (!$request) continue;
        $running=(!$status || $status['state']==='running' || $status['state']==='queued');
        if (time()-$request['created_at']>86400 && !$running) { remove_job($jobs.'/'.$id); continue; }
        if ($running) {
            $active++;
            if ($request['username']===$uid) problem(409,'export_already_running');
        }
    }
    if ($active>=4) problem(409,'export_busy');
    $id=new_job_id($config,$root); $job=$jobs.'/'.$id;
    if (!mkdir($job,0700)) problem(503,'job_storage_failed');
    save_object($job.'/request.json',array('username'=>$uid,'approvals'=>$approvals,'mail'=>$mail,'created_at'=>time()));
    save_object($job.'/status.json',array('protocol'=>'segio-live-archive/1','username'=>$uid,'job'=>$id,'state'=>'queued','phase'=>'queued'));
    flock($gate,LOCK_UN); fclose($gate);
    $started=run_fixed_worker($config,$root,array('--config',$root.'/config.json','--job',$id,'--detach'),false);
    if ($started['state']!=='ok') {
        $runtime_failure=public_worker_result($started);
        save_object($job.'/status.json',array('protocol'=>'segio-live-archive/1','username'=>$uid,'job'=>$id,'state'=>'failed','phase'=>'export_process_unavailable'));
        problem(503,'export_process_unavailable');
    }
    answer(202,array('protocol'=>'segio-live-archive/1','username'=>$uid,'job'=>$id,'state'=>'queued'));
}
$id=isset($_GET['job'])?$_GET['job']:'';
if (!is_string($id) || !preg_match('/\A[a-f0-9]{32}\z/',$id)) problem(400,'invalid_job');
$job=$jobs.'/'.$id;
if (is_link($job)) problem(404,'job_not_found');
$request=object_file($job.'/request.json');
if (!$request || $request['username']!==$uid) problem(404,'job_not_found');
$status=object_file($job.'/status.json');
if ($action==='status') {
    if (!$status) problem(503,'job_status_unavailable');
    answer(200,$status);
}
if ($action==='cleanup') {
    if ($status && ($status['state']==='ready' || $status['state']==='failed')) remove_job($job);
    else file_put_contents($job.'/cancel','cancel');
    answer(200,array('protocol'=>'segio-live-archive/1','username'=>$uid,'state'=>'cleaned'));
}
if (!$status || $status['state']!=='ready') problem(409,'export_not_ready');
$file=$job.'/archive.zip';
if (is_link($file) || !is_file($file) || filesize($file)!=$status['bytes']) problem(503,'archive_unavailable');
header('Content-Type: application/zip'); header('Content-Length: '.$status['bytes']);
header('Content-Disposition: attachment; filename="segio-'.$uid.'.zip"');
if ($method==='HEAD') exit;
@set_time_limit(0); readfile($file);
