PYTEST_COV_ARGS =

# Use the installed Python 3.12 interpreter. Override for another platform
# with e.g. `make PYTHON_BIN=python3.12`.
PYTHON_BIN ?= py -3.12
PIP_BIN ?= $(PYTHON_BIN) -m pip
PYTEST_BIN ?= $(PYTHON_BIN) -m pytest
RUFF_BIN ?= $(PYTHON_BIN) -m ruff
PYBABEL_BIN ?= pybabel
UV_BIN ?= uv

FORCE: ;

deps:
	$(PIP_BIN) install -r requirements-dev.txt

deps-runtime:
	$(PIP_BIN) install -r requirements.txt

lock: requirements.txt requirements-dev.txt

lint:
	$(RUFF_BIN) check

lint-fix:
	$(RUFF_BIN) check --fix

format:
	$(RUFF_BIN) format

requirements.txt: requirements.in
	$(UV_BIN) pip compile requirements.in --python-version 3.12 -o requirements.txt

requirements-dev.txt: requirements-dev.in requirements.txt
	$(UV_BIN) pip compile requirements-dev.in --python-version 3.12 -o requirements-dev.txt

deps-tree:
	$(PYTHON_BIN) -m pipdeptree --warn fail

test: test_all

test_all:
	$(PYTEST_BIN) $(PYTEST_COV_ARGS)

test_all_parallel:
	# TODO: fix this
	$(PYTEST_BIN) -n 8 $(PYTEST_COV_ARGS)

test_single:
	$(PYTEST_BIN) tests/api/test_file_api.py $(PYTEST_COV_ARGS)

test_logging:
	#--capture=no
	$(PYTEST_BIN) --capture=sys --log-cli-level=DEBUG tests/base/test_logging.py $(PYTEST_COV_ARGS)

babel-update-po:
	$(PYBABEL_BIN) extract -F babel.cfg -k lazy_gettext -k hardcode_text -o messages.pot app
	$(PYBABEL_BIN) update -i messages.pot -d app/translations

babel-update-mo: babel-update-po
	$(PYBABEL_BIN) compile -d app/translations

babel-translate-po:
	$(PYTHON_BIN) app/scripts/fill_zh_translations.py
	$(PYTHON_BIN) app/scripts/fill_en_translations.py
