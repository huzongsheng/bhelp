.PHONY: test build install

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

build:
	python3 -m zipapp src -m "bhelp.cli:entrypoint" -p "/usr/bin/env python3" -o dist/bhelp
	chmod 0755 dist/bhelp

install:
	sh scripts/install.sh
