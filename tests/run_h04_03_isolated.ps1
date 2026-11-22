[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$composeFile = (Resolve-Path (Join-Path $PSScriptRoot "h04-03\compose.yaml")).Path
$projectName = "h04-03-test-$([Guid]::NewGuid().ToString('N').ToLowerInvariant())"
$composeBase = @("compose", "--project-name", $projectName, "--file", $composeFile)
$composeInvoked = $false
$testExitCode = 1

try {
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Engine no está accesible desde esta sesión de PowerShell."
    }

    $configText = & docker @composeBase config --format json 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose config falló: $($configText -join [Environment]::NewLine)"
    }
    $config = ($configText -join [Environment]::NewLine) | ConvertFrom-Json
    $serviceNames = @($config.services.PSObject.Properties.Name | Sort-Object)
    if (($serviceNames -join ",") -ne "pytest,redis") {
        throw "El Compose debe contener solamente pytest y redis."
    }
    if ($config.volumes -and $config.volumes.PSObject.Properties.Count -gt 0) {
        throw "El Compose declara volúmenes persistentes; se cancela por seguridad."
    }
    foreach ($serviceName in $serviceNames) {
        $service = $config.services.$serviceName
        if ($null -ne $service.volumes) {
            foreach ($mount in $service.volumes) {
                if ($serviceName -ne "redis" -or $mount.type -ne "tmpfs" -or $mount.target -ne "/data") {
                    throw "Solo se permite un tmpfs efímero en /data para Redis."
                }
            }
        }
        if ($service.ports -and $service.ports.Count -gt 0) {
            throw "El servicio $serviceName publica puertos al host."
        }
        if ($service.privileged -eq $true) {
            throw "El servicio $serviceName solicita modo privileged."
        }
        if (($service.networks.PSObject.Properties.Name -join ",") -ne "h04_test") {
            throw "El servicio $serviceName no está limitado a la red h04_test."
        }
    }
    if ($config.networks.h04_test.internal -ne $true -or $config.networks.h04_test.external -eq $true) {
        throw "La red de prueba debe ser interna y no externa."
    }
    if (@($config.services.redis.volumes).Count -ne 1) {
        throw "Redis debe usar un tmpfs en /data para evitar volúmenes de imagen persistentes."
    }

    $existingContainers = & docker ps -aq --filter "label=com.docker.compose.project=$projectName"
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo comprobar la disponibilidad del nombre de proyecto temporal."
    }
    $existingNetworks = & docker network ls -q --filter "label=com.docker.compose.project=$projectName"
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo comprobar la red del proyecto temporal."
    }
    if ($existingContainers -or $existingNetworks) {
        throw "El identificador temporal ya existe; no se reutilizarán recursos previos."
    }

    Write-Host "Proyecto aislado: $projectName"
    Write-Host "Compose: $composeFile"
    Write-Host "Redis solo será accesible dentro de la red interna temporal; no hay puertos ni volúmenes."

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
            Write-Warning "docker compose down no pudo completar la limpieza del proyecto temporal."
            if ($testExitCode -eq 0) {
                $testExitCode = 1
            }
        }
        else {
            $remainingContainers = & docker ps -aq --filter "label=com.docker.compose.project=$projectName"
            $remainingNetworks = & docker network ls -q --filter "label=com.docker.compose.project=$projectName"
            if ($remainingContainers -or $remainingNetworks) {
                Write-Warning "Quedaron recursos con la etiqueta del proyecto temporal; revisar $projectName."
                if ($testExitCode -eq 0) {
                    $testExitCode = 1
                }
            }
            else {
                Write-Host "Contenedores y red temporales eliminados."
            }
        }
    }
}

exit $testExitCode
