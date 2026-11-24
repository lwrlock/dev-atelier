.PHONY: test install-apps

install-apps:
	pip install -q -e apps/flowforge -e apps/cortexscope -e apps/hexward -e . --no-deps

test:
	python3 -m pytest -v tests/
	$(MAKE) -C apps/flowforge test
	$(MAKE) -C apps/cortexscope test
	$(MAKE) -C apps/hexward test
