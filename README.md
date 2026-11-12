# requirements

This repo contains the tooling required by the FreeBSD ports for OpenStack services.

It helps generating the distfiles so that all the ports can be build offline:

- upper constraints for the OpenStack release are converted into `pyproject.toml` using the `uc2toml.py` script
- uv is used to resolve the dependencies and the distfile sets are generated from the lock file by `lock2port.py`
- any rust crates inside the Python packages are also resolved and added to the distfiles by `crates.py`

The actual ports are in the [stackbsd/ports](https://github.com/stackbsd/ports) repo.

## Port design

FreeBSD ports typically split Python packages into separate ports, however, OpenStack has a strict dependency constraint that moves with each release.
Many of the packages have conflicting versions in the upstream ports tree.

The stackbsd ports install into their own venv `/usr/local/libexec/openstack`, which is not typical for Python ports in the ports tree but a precedent that does exist in some other language ports.

The runtime port holds all the common dependencies between services, like oslo libs, gunicorn, amqp etc.

Each per-service port holds only dependencies unique to itself, so e.g. for nova that means numpy and websockify.

This is a compromise to create a working OpenStack port that can be installed on regular FreeBSD systems without a significant engineering effort.
