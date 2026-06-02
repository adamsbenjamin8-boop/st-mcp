# ST MCP — Auto-configure Claude desktop app
# Adds the servicetitan-writer MCP server to the user's Claude config
# Preserves all existing preferences

$ErrorActionPreference = 'Stop'

try {
    # Resolve the logged-in user's profile (works even when running as admin)
    $userProfile = $env:USERPROFILE
    if (-not $userProfile) {
        # Fallback: ask WMI for the interactive session user
        $loggedIn = (Get-WMIObject Win32_ComputerSystem).UserName
        if ($loggedIn -match '\\') { $loggedIn = $loggedIn.Split('\')[1] }
        $userProfile = "C:\Users\$loggedIn"
    }

    $configDir  = "$userProfile\AppData\Roaming\Claude"
    $configFile = "$configDir\claude_desktop_config.json"
    $docsPath   = "$userProfile\Documents\Claude"

    # Ensure config directory exists
    if (-not (Test-Path $configDir)) {
        New-Item -ItemType Directory -Force -Path $configDir | Out-Null
    }

    # Read existing config or start fresh
    if (Test-Path $configFile) {
        $raw    = Get-Content $configFile -Raw -Encoding UTF8
        $config = $raw | ConvertFrom-Json
    } else {
        $config = [PSCustomObject]@{}
    }

    # Set coworkUserFilesPath to this user's Documents\Claude
    if (-not ($config | Get-Member -Name 'coworkUserFilesPath' -ErrorAction SilentlyContinue)) {
        $config | Add-Member -NotePropertyName 'coworkUserFilesPath' -NotePropertyValue $docsPath
    } else {
        $config.coworkUserFilesPath = $docsPath
    }

    # Ensure mcpServers object exists
    if (-not ($config | Get-Member -Name 'mcpServers' -ErrorAction SilentlyContinue)) {
        $config | Add-Member -NotePropertyName 'mcpServers' -NotePropertyValue ([PSCustomObject]@{})
    }

    # Build the servicetitan-writer entry
    $stEntry = [PSCustomObject]@{
        command = 'python'
        args    = @('C:\Program Files\ST_MCP\servicetitan_writer.py')
        cwd     = 'C:\Program Files\ST_MCP'
        env     = [PSCustomObject]@{
            ST_CLIENT_ID     = 'cid.hjgexhrgh6wtgsbxj8k2p1leh'
            ST_CLIENT_SECRET = 'cs1.h7fhi8vwvb28swcx3jj58cbov1vsbvasuag8drrx55wmlms8aq'
            ST_APP_KEY       = 'ak1.w2ugo0fhvb0q8o855dpptfpmw'
            ST_TENANT_ID     = '1842637205'
        }
    }

    # Add or overwrite the servicetitan-writer key
    if ($config.mcpServers | Get-Member -Name 'servicetitan-writer' -ErrorAction SilentlyContinue) {
        $config.mcpServers.'servicetitan-writer' = $stEntry
    } else {
        $config.mcpServers | Add-Member -NotePropertyName 'servicetitan-writer' -NotePropertyValue $stEntry
    }

    # Write back — depth 10 so nested objects serialize correctly
    $config | ConvertTo-Json -Depth 10 | Set-Content $configFile -Encoding UTF8

    Write-Output "Claude config updated: $configFile"
    exit 0

} catch {
    Write-Output "Warning: Could not update Claude config — $($_.Exception.Message)"
    exit 0   # Non-fatal — app still works, user just needs to add config manually
}
