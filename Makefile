PYTEST_ARGS = -m 'not egta'
PYLINT_ARGS =
PYTHON = python3


help:
	@echo "usage: make <tag>"
	@echo
	@echo "setup    - setup for development"
	@echo "check    - check for comformance to pep8 standards"
	@echo "docs     - generate documentation"
	@echo "test     - run quick tests"
	@echo "test-all - run all tests"
	@echo "publish  - publish project to pypi"

setup:
	$(PYTHON) -m venv .
	bin/pip install -U pip setuptools
	bin/pip install -e '.[dev]'

test-all: PYTEST_ARGS += -m ''
test-all: test

test:
	bin/pytest $(PYTEST_ARGS) test --cov egtaonline --cov test 2>/dev/null

check:
	bin/pylint $(PYLINT_ARGS) egtaonline test

docs:
	bin/sphinx-apidoc -fo sphinx egtaonline
	bin/python setup.py build_sphinx -b html

publish:
	rm -rf dist
	bin/python setup.py sdist bdist_wheel
	bin/twine upload -u strategic.reasoning.group dist/*

travis: PYTEST_ARGS += -v -n2
travis: PYLINT_ARGS += -d fixme -j 2
travis: check test

clean:
	rm -rf bin include lib lib64 man share pyvenv.cfg build dist pip-selfcheck.json __pycache__ egtaonlineapi.egg-info

.PHONY: setup test-all test todo check format docs publish clean
