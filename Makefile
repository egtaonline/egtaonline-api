TEST_ARGS = -m 'not egta'
FILES = egtaonline test setup.py
PYTHON = python3


help:
	@echo "usage: make <tag>"
	@echo
	@echo "setup    - setup for development"
	@echo "todo     - check for todo flags"
	@echo "check    - check for comformance to pep8 standards"
	@echo "format   - autoformat python files"
	@echo "docs     - generate documentation"
	@echo "test     - run quick tests"
	@echo "test-all - run all tests"
	@echo "publish  - publish project to pypi"

setup:
	$(PYTHON) -m venv .
	bin/pip install -U pip setuptools
	bin/pip install -e '.[dev]'

test-all: TEST_ARGS += -m ''
test-all: test

test:
	bin/pytest $(TEST_ARGS) test --cov egtaonline --cov test 2>/dev/null

todo:
	grep -nrIF -e TODO -e XXX -e FIXME --color=always $(FILES)

check:
	bin/flake8 $(FILES)

format:
	bin/autopep8 -ri $(FILES)

docs:
	bin/sphinx-apidoc -fo sphinx egtaonline
	bin/python setup.py build_sphinx -b html

publish:
	rm -rf dist
	bin/python setup.py sdist bdist_wheel
	bin/twine upload -u strategic.reasoning.group dist/*

clean:
	rm -rf bin include lib lib64 man share pyvenv.cfg build dist pip-selfcheck.json __pycache__ egtaonlineapi.egg-info

.PHONY: setup test-all test todo check format docs publish clean
