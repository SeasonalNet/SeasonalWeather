# Forgejo Runner Docker access

The P2-09 image gate needs a real Docker endpoint because it builds, inspects,
and executes every declared image profile. A Forgejo Docker-backend job runs in
an unprivileged container by default. Installing Docker Engine inside that job
container is not sufficient: BuildKit requires mount capabilities that the job
does not own.

SeasonalWeather therefore assigns image work only to the dedicated
`victus-builder` runner. Its workflow job installs only `docker-ce-cli` and the
Buildx plugin, verifies the supplied endpoint before installing Python
dependencies, and then runs `make phase2-images`. The ordinary `victus-fast`
runner runs `make check` without Docker access. No other runner needs the P2-09
Docker authority.

## Recommended isolated DIND topology

`victus-builder` uses the isolated `dind-builder` daemon. This follows the
[Forgejo 15 Docker access guide](https://forgejo.org/docs/v15.0/admin/actions/docker-access/).
Pin the DIND image to the deployment's reviewed digest.

On the runner host, the Forgejo-documented topology is equivalent to:

```bash
docker run \
  --publish 127.0.0.1:2376:2375 \
  --detach \
  --privileged \
  --restart always \
  --name dind-builder \
  docker:dind \
  dockerd --host tcp://0.0.0.0:2375 --tls=false
```

Configure the runner to use that daemon for container creation and expose the
same endpoint to job steps:

```yaml
runner:
  capacity: 1
  envs:
    DOCKER_HOST: tcp://dind-builder.docker.internal:2375

container:
  docker_host: tcp://127.0.0.1:2376
  options: --add-host=dind-builder.docker.internal:host-gateway
```

Restart the runner after changing its configuration, then verify from a
disposable trusted job that `docker info` and `docker buildx inspect
--bootstrap` succeed. The SeasonalWeather CI preflight performs those same
checks and fails with this document's path before the Python suite starts when
the endpoint is absent.

Keep `victus-builder` at capacity one unless each concurrent builder has an
independent DIND daemon. Jobs sharing a daemon can inspect or mutate one
another's images and containers. Protect the loopback-only plaintext endpoint,
periodically maintain DIND storage, and do not expose the host Docker socket as
an incidental workflow workaround.

## Other supported runner authorities

Forgejo also documents an explicit `container.docker_host: automount` mode and
LXC/VM runner isolation. Socket automount exposes the host Docker authority to
workflow code and must be an explicit administrator security decision; this
repository does not mount `/var/run/docker.sock` from workflow YAML. LXC or a
dedicated VM may be preferable when stronger host isolation is required.
