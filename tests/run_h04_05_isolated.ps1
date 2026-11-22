[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$composeFile = (Resolve-Path (Join-Path $PSScriptRoot "h04-05\compose.yaml")).Path
$projectName = "h04-05-test-$([Guid]::NewGuid().ToString('N').ToLowerInvariant())"
$composeBase = @("compose", "--project-name", $projectName, "--file", $composeFile)
$composeInvoked = $false
$testExitCode = 1

try {
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Engine no esta accesible desde esta sesion de PowerShell."
    }

    $configText = & docker @composeBase config --format json 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose config fallo: $($configText -join [Environment]::NewLine)"
    }
    $config = ($configText -join [Environment]::NewLine) | ConvertFrom-Json
    $serviceNames = @($config.services.PSObject.Properties.Name | Sort-Object)
    if (($serviceNames -join ",") -ne "kafka,pytest,redis") {
        throw "El Compose H04-05 debe contener unicamente kafka, pytest y redis."
    }
    if ($config.volumes -and $config.volumes.PSObject.Properties.Count -gt 0) {
        throw "El Compose declara volumenes persistentes; se cancela por seguridad."
    }
    foreach ($serviceName in $serviceNames) {
        $service = $config.services.$serviceName
        if ($service.ports -and $service.ports.Count -gt 0) {
            throw "El servicio $serviceName publica puertos al host."
        }
        if ($service.privileged -eq $true) {
            throw "El servicio $serviceName solicita modo privileged."
        }
        if (($service.networks.PSObject.Properties.Name -join ",") -ne "h04_test") {
            throw "El servicio $serviceName no esta limitado a la red h04_test."
        }
        if ($service.volumes) {
            foreach ($mount in $service.volumes) {
                if ($mount.type -ne "tmpfs") {
                    throw "Solo se permiten montajes tmpfs desechables ($serviceName)."
                }
            }
        }
    }
    if ($config.networks.h04_test.internal -ne $true -or $config.networks.h04_test.external -eq $true) {
        throw "La red de pruebas debe ser interna y no externa."
    }

    $existingContainers = & docker ps -aq --filter "label=com.docker.compose.project=$projectName"
    if ($LASTEXITCODE -ne 0) { throw "No se pudo verificar el proyecto temporal." }
    $existingNetworks = & docker network ls -q --filter "label=com.docker.compose.project=$projectName"
    if ($LASTEXITCODE -ne 0) { throw "No se pudo verificar la red temporal." }
    if ($existingContainers -or $existingNetworks) {
        throw "El identificador temporal ya existe; no se reutilizaran recursos previos."
    }

    Write-Host "Proyecto aislado: $projectName"
    Write-Host "Kafka y Redis son temporales, sin puertos publicados ni volumenes persistentes."
    $composeInvoked = $true
    & docker @composeBase up --build --abort-on-container-exit --exit-code-from pytest
    $testExitCode = $LASTEXITCODE
}
catch {
    Write-Error $_
    $testExitCode = 1
}
finally {
    if ($composeInvoked) {
        Write-Host "Limpiando exclusivamente el proyecto $projectName..."
        & docker @composeBase down --remove-orphans
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "No se pudo completar la limpieza del proyecto temporal."
            if ($testExitCode -eq 0) { $testExitCode = 1 }
        }
        else {
            $remainingContainers = & docker ps -aq --filter "label=com.docker.compose.project=$projectName"
            $remainingNetworks = & docker network ls -q --filter "label=com.docker.compose.project=$projectName"
            if ($remainingContainers -or $remainingNetworks) {
                Write-Warning "Quedaron recursos etiquetados con $projectName; revisar manualmente."
                if ($testExitCode -eq 0) { $testExitCode = 1 }
            }
            else {
                Write-Host "Contenedores y red temporales eliminados."
            }
        }
    }
}

exit $testExitCode
