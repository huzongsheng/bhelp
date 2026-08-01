BHELP_PYTHON ?= python3
BHELP_ZIPAPP := dist/bhelp

.PHONY: test build smoke check install clean

test:
	PYTHONPATH=src $(BHELP_PYTHON) -m unittest discover -s tests -v

build:
	$(BHELP_PYTHON) scripts/build_zipapp.py --output $(BHELP_ZIPAPP)

smoke: build
	$(BHELP_PYTHON) $(BHELP_ZIPAPP) --help >/dev/null

check: clean
	$(MAKE) test
	$(MAKE) smoke

install:
	sh scripts/install.sh

clean:
	rm -f $(BHELP_ZIPAPP)
