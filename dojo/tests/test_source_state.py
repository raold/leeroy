"""Exercise the actual PowerShell build admission against real dirty git trees."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


HELPER = Path(__file__).resolve().parents[2] / "scripts/dojo/source-state.ps1"


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell is required for the Windows build helper")
class CleanBuildTest(unittest.TestCase):
    def test_dirty_build_then_revert_cannot_borrow_a_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "check.ps1"
            script.write_text(r'''
param($Helper, $Root)
Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'
. $Helper
function Check($Condition, $Message) { if (-not $Condition) { throw $Message } }
$repo = Join-Path $Root 'repo'
New-Item -ItemType Directory $repo | Out-Null
git -C $repo init -q
git -C $repo config user.email test@example.invalid
git -C $repo config user.name Test
Set-Content "$repo/tracked" 'original'
git -C $repo add tracked
git -C $repo commit -qm initial
$roots = @{ source=$repo; core=$repo; playerbots=$repo }
$clean = Get-DojoCleanRevisions $roots
Assert-DojoSameRevisions $clean (Get-DojoCleanRevisions $roots)
foreach ($kind in @('modified', 'staged', 'untracked')) {
    if ($kind -eq 'untracked') { Set-Content "$repo/new" 'new' }
    else { Set-Content "$repo/tracked" 'changed' }
    if ($kind -eq 'staged') { git -C $repo add tracked }
    $rejected = $false
    try { $null = Get-DojoCleanRevisions $roots } catch { $rejected = $_.Exception.Message -like '*dirty*' }
    Check $rejected "accepted $kind sources"
    git -C $repo reset -q --hard HEAD
    if (Test-Path "$repo/new") { Remove-Item -LiteralPath "$repo/new" }
}
Set-Content "$repo/tracked" 'next revision'
git -C $repo add tracked
git -C $repo commit -qm next
$rejected = $false
try { Assert-DojoSameRevisions $clean (Get-DojoCleanRevisions $roots) } catch { $rejected = $true }
Check $rejected 'accepted changed HEAD during build'
$build = Join-Path $Root 'build'
New-Item -ItemType Directory "$build/bin/RelWithDebInfo" -Force | Out-Null
Set-Content "$build/bin/RelWithDebInfo/worldserver.exe" 'fixture binary'
Check (-not (Test-DojoTrustedBuild $build)) 'trusted missing receipt'
Set-Content "$build/dojo-build.json" '{}'
Check (-not (Test-DojoTrustedBuild $build)) 'trusted malformed receipt'
$receipt = @{ clean_revisions=$clean; exit_code=0; problems=@(); artifacts=@{
    'worldserver.exe'=@{sha256=(Get-FileHash "$build/bin/RelWithDebInfo/worldserver.exe").Hash.ToLowerInvariant()}} }
$receipt | ConvertTo-Json -Depth 5 | Set-Content "$build/dojo-build.json"
Check (Test-DojoTrustedBuild $build) 'rejected attested prior build'
Set-Content "$build/dojo-build.pending.json" '{}'
Check (-not (Test-DojoTrustedBuild $build)) 'trusted interrupted build after source revert'
Remove-Item -LiteralPath "$build/dojo-build.pending.json"
Set-Content "$build/bin/RelWithDebInfo/worldserver.exe" 'dirty build output'
Check (-not (Test-DojoTrustedBuild $build)) 'trusted replaced binary'
''', encoding="utf-8")
            result = subprocess.run(["pwsh", "-NoProfile", "-File", str(script), str(HELPER), str(root)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
