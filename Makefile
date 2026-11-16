.PHONY: test gui windows clean

test:
	uv run pytest

# The window, for development: its own light environment, using this repo's
# .venv (which has the engine) instead of installing one.
gui:
	EQ_HD_ENGINE_PYTHON=$(CURDIR)/.venv/bin/python UV_PROJECT_ENVIRONMENT=$(CURDIR)/.venv-gui \
		uv run --no-default-groups --group gui eq-hd-gui

# The Windows download (dist/eq-hd-textures-<version>-windows.zip); runs on Linux.
windows:
	uv run python tools/build_windows.py

# Remove everything downloaded or temporary: the Python environments (.venv,
# .venv-gui; uv recreates them on the next `uv run`, from its cache), the
# downloaded model (re-downloaded by the next render), build/, dist/, work/
# and caches. Never touches output/ (the rendered archives and their state).
clean:
	rm -rf "$(CURDIR)/.venv" "$(CURDIR)/.venv-gui" "$(CURDIR)/build" "$(CURDIR)/dist" "$(CURDIR)/work" \
		"$(CURDIR)/.pytest_cache"
	rm -f "$(CURDIR)"/models/*.pth "$(CURDIR)"/models/*.part
	find "$(CURDIR)/src" "$(CURDIR)/tests" "$(CURDIR)/tools" -name __pycache__ -type d -prune -exec rm -rf {} +
