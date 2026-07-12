.PHONY: fmt lint test verify-examples

fmt:
	python3 -m ruff check --fix scripts tests
	python3 -m ruff format scripts tests

lint:
	python3 -m ruff check scripts tests
	python3 -m py_compile scripts/*.py scripts/extractors/*.py scripts/chief_wiggum/*.py
	python3 scripts/check_portability.py
	python3 scripts/check_patterns.py

test:
	python3 -m pytest

verify-examples:
	python3 scripts/formal_models.py validate docs/formal-methods/examples/order-lifecycle.state-machine.json
	python3 scripts/formal_models.py validate docs/formal-methods/examples/order-lifecycle.contracts.json
	python3 -m pytest tests/test_generated_examples.py
