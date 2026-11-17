[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'VDD CI setup requires an elevated administrator shell.'
}
$work = Join-Path $env:RUNNER_TEMP ('webskrap-vdd-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $work | Out-Null

function Get-VerifiedArchive([string]$Url, [string]$Name, [string]$Hash) {
    $archive = Join-Path $work "$Name.zip"
    Write-Host "Downloading pinned $Name archive: $Url"
    Invoke-WebRequest -Uri $Url -OutFile $archive
    $actual = (Get-FileHash -Path $archive -Algorithm SHA256).Hash
    if ($actual -ne $Hash) {
        throw "SHA-256 mismatch for $Name. Expected $Hash; got $actual."
    }
    $destination = Join-Path $work $Name
    Expand-Archive -Path $archive -DestinationPath $destination
    return $destination
}

try {
    $driver = Get-VerifiedArchive `
        'https://github.com/VirtualDrivers/Virtual-Display-Driver/releases/download/25.7.23/VirtualDisplayDriver-x86.Driver.Only.zip' `
        'driver' 'e24210692b442b39af763536330ce78b423f19342b7a7792c26de3944e418b3a'
    $nefcon = Get-VerifiedArchive `
        'https://github.com/nefarius/nefcon/releases/download/v1.21.1/nefcon_v1.21.1.zip' `
        'nefcon' '28292f4b1417484db6ad23c47ffc18ea99d3de3bdb3bbbe60c8ed11703a3780a'
    $settingsDirectory = 'C:\VirtualDisplayDriver'
    New-Item -ItemType Directory -Path $settingsDirectory -Force | Out-Null
    $settingsPath = Join-Path $settingsDirectory 'vdd_settings.xml'
    if (Test-Path $settingsPath) {
        throw "Refusing to overwrite existing VDD settings at $settingsPath. Use a fresh CI runner."
    }
    $templates = @(Get-ChildItem -Path $driver -Recurse -Filter 'vdd_settings.xml')
    if ($templates.Count -ne 1) { throw 'Pinned driver archive has no unique settings template.' }
    [xml]$settings = Get-Content -Path $templates[0].FullName -Raw
    $settings.SelectSingleNode('/vdd_settings/monitors/count').InnerText = '2'
    $global = $settings.SelectSingleNode('/vdd_settings/global')
    $global.RemoveAll()
    $rate = $settings.CreateElement('g_refresh_rate')
    $rate.InnerText = '60'
    $global.AppendChild($rate) | Out-Null
    $resolutions = $settings.SelectSingleNode('/vdd_settings/resolutions')
    $resolutions.RemoveAll()
    foreach ($size in @(@(1920, 1080), @(1366, 768), @(1280, 720), @(1280, 800), @(1440, 900))) {
        $resolution = $settings.CreateElement('resolution')
        foreach ($entry in @(@('width', $size[0]), @('height', $size[1]), @('refresh_rate', 60))) {
            $element = $settings.CreateElement($entry[0])
            $element.InnerText = [string]$entry[1]
            $resolution.AppendChild($element) | Out-Null
        }
        $resolutions.AppendChild($resolution) | Out-Null
    }
    $settings.Save($settingsPath)
    Write-Host "Wrote two-monitor configuration to $settingsPath before driver installation."
    $catalog = @(Get-ChildItem -Path $driver -Recurse -Filter 'mttvdd.cat')
    $inf = @(Get-ChildItem -Path $driver -Recurse -Filter 'MttVDD.inf')
    $installer = Join-Path $nefcon 'x64\nefconc.exe'
    if ($catalog.Count -ne 1 -or $inf.Count -ne 1 -or -not (Test-Path $installer)) {
        throw 'Pinned archives do not contain the expected catalog, INF, or x64 nefconc.exe.'
    }
    $signature = Get-AuthenticodeSignature -FilePath $catalog[0].FullName
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate) {
        throw "Driver catalog signature verification failed: $($signature.Status)."
    }
    $certificate = $signature.SignerCertificate
    $certificatePath = Join-Path $work ($certificate.Thumbprint + '.cer')
    [IO.File]::WriteAllBytes($certificatePath, $certificate.Export([Security.Cryptography.X509Certificates.X509ContentType]::Cert))
    Import-Certificate -FilePath $certificatePath -CertStoreLocation 'Cert:\LocalMachine\TrustedPublisher' | Out-Host
    Write-Host 'Installing the pinned Virtual Display Driver.'
    & $installer install $inf[0].FullName 'Root\MttVDD'
    if ($LASTEXITCODE -ne 0) { throw "nefconc install failed with exit code $LASTEXITCODE." }
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class WebSkrapVdd {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct Device {
        public uint cb;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string Name;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string Description;
        public uint Flags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string Id;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string Key;
    }
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern bool EnumDisplayDevices(string parent, uint index, ref Device device, uint flags);
    static List<Device> Devices(string parent) {
        var devices = new List<Device>();
        for (uint i = 0; ; i++) {
            var device = new Device();
            device.cb = (uint)Marshal.SizeOf(typeof(Device));
            if (!EnumDisplayDevices(parent, i, ref device, 0)) break;
            devices.Add(device);
        }
        return devices;
    }
    static bool IsVdd(Device device) {
        return String.Equals(device.Description, "Virtual Display Driver", StringComparison.OrdinalIgnoreCase)
            || String.Equals(device.Id, @"Root\MttVDD", StringComparison.OrdinalIgnoreCase)
            || (device.Id ?? "").StartsWith(@"Root\MttVDD\", StringComparison.OrdinalIgnoreCase);
    }
    public static int Count() {
        int count = 0;
        foreach (var adapter in Devices(null)) {
            bool found = IsVdd(adapter);
            foreach (var monitor in Devices(adapter.Name)) found |= IsVdd(monitor);
            if (found) count++;
        }
        return count;
    }
}
'@
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        $count = [WebSkrapVdd]::Count()
        Write-Host "Enumerated $count VDD monitors; waiting for two."
        if ($count -ge 2) { break }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $deadline)
    Get-CimInstance Win32_DesktopMonitor | Format-List | Out-Host
    Get-PnpDevice -Class Display, Monitor | Format-Table -AutoSize | Out-Host
    if ($count -lt 2) { throw "Timed out after 60 seconds: found $count VDD monitors, expected two." }
    Write-Host 'VDD CI setup complete: two monitors are ready.'
} catch {
    Write-Host "VDD CI setup failed: $_"
    Get-CimInstance Win32_DesktopMonitor -ErrorAction Continue | Format-List | Out-Host
    Get-PnpDevice -Class Display, Monitor -ErrorAction Continue | Format-Table -AutoSize | Out-Host
    throw
} finally {
    Remove-Item -Path $work -Recurse -Force
}
