$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    & python -B -m resume_workflow @args
    $workflowExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $workflowExitCode
