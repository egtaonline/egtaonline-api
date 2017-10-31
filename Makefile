TEST_ARGS = 
FILES = egtaonline test setup.py
PYTHON = python


help:
	@echo "usage: make <tag>"
	@echo
	@echo "setup  - setup for development"
	@echo "ubuntu-reqs - install required files on ubuntu (requires root)"
	@echo "todo   - check for todo flags"
	@echo "check  - check for comformance to pep8 standards"
	@echo "format - autoformat python files"
	@echo "test   - run tests"

setup:
	$(PYTHON) -m venv .
	bin/pip install -U pip setuptools
	bin/pip install -e '.[dev]'

test-all: TEST_ARGS += -m ''
test-all: test

test:
	bin/pytest $(TEST_ARGS) test --cov egtaonline --cov test 2>/dev/null

todo:
	grep -nrIF -e TODO -e XXX -e FIXME * --exclude-dir=lib --exclude-dir=game_analysis --exclude=Makefile --color=always

check:
	bin/flake8 $(FILES)

format:
	bin/autopep8 -ri $(FILES)

docs:
	bin/python setup.py build_sphinx -b html


upload:
	cp ~/.pypirc ~/.pypirc.bak~ || touch ~/.pypirc.bak~
	echo '[distutils]\nindex-servers =\n    pypi\n\n[pypi]\nusername: strategic.reasoning.group' > ~/.pypirc
	bin/python setup.py sdist bdist_wheel upload; mv ~/.pypirc.bak~ ~/.pypirc

clean:
	rm -rf bin include lib lib64 man share pyvenv.cfg build dist pip-selfcheck.json __pycache__ egtaonlineapi.egg-info

.PHONY: docs clean test coverage
