.PHONY: test lint run

test:
	node --test tests/frontend.test.mjs
	python3 -m pytest -v

run:
	hexward serve --host 127.0.0.1 --port 11460
