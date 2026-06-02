$action = New-ScheduledTaskAction -Execute "C:\Users\Ben\AppData\Local\Python\bin\python.exe" `
    -Argument "C:\ST\st_cache_sync.py" `
    -WorkingDirectory "C:\ST"

$env_vars = "ST_CLIENT_ID=cid.hjgexhrgh6wtgsbxj8k2p1leh;ST_CLIENT_SECRET=cs1.h7fhi8vwvb28swcx3jj58cbov1vsbvasuag8drrx55wmlms8aq;ST_APP_KEY=ak1.w2ugo0fhvb0q8o855dpptfpmw;ST_TENANT_ID=1842637205"

$trigger = New-ScheduledTaskTrigger -Daily -At "02:05AM"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest

Unregister-ScheduledTask -TaskName "ST_Cache_Sync" -Confirm:$false -ErrorAction SilentlyContinue

$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
$task.Actions[0].EnvironmentVariables = $env_vars
Register-ScheduledTask -TaskName "ST_Cache_Sync" -InputObject $task

Write-Host "Scheduled task updated with env vars"
