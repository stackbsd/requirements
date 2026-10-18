SERIES?=	2026.1
PYTHON_FULL?=	3.12.12
PORTS_OUT?=	out/net
PORTS?=		ports.toml
PORTS_TREE?=	../ports
UC_URL=		https://raw.githubusercontent.com/openstack/requirements/stable/${SERIES}/upper-constraints.txt

LOCK2PORT=	uv run tools/lock2port.py --lock uv.lock --ports ${PORTS} \
		--out ${PORTS_OUT} --python-full-version ${PYTHON_FULL}

all: constraints lock ports

constraints:
	curl -sfL ${UC_URL} -o constraints/${SERIES}.txt
	uv run tools/uc2toml.py constraints/${SERIES}.txt pyproject.toml

lock:
	uv lock

# generate the distfile metadata of every port in ports.toml
ports:
	${LOCK2PORT}

# install the same into the ports checkout and version each service Makefile
sync:
	${LOCK2PORT} --ports-tree ${PORTS_TREE}

.PHONY: all constraints lock ports sync
