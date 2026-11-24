.PHONY: install serve test clean

install:
	python3 -m pip install -e . --no-build-isolation --no-deps

serve:
	cortexscope serve --host 127.0.0.1 --port 11435 --upstream http://127.0.0.1:11434

test:
	node --test tests/frontend.test.mjs
	python3 -m pytest -v

install-service:
	mkdir -p $(HOME)/.config/systemd/user
	cp deploy/cortexscope.service $(HOME)/.config/systemd/user/cortexscope.service
	systemctl --user daemon-reload
	systemctl --user enable --now cortexscope.service

clean:
	rm -rf .pytest_cache src/*.egg-info
