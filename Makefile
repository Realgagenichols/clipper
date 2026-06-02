# clipper dev wrapper.
#
# Two workarounds baked into every target:
#   1. UV_NO_EDITABLE=0 — overrides a globally-set UV_NO_EDITABLE=1 so the
#      project is installed editable (otherwise new module files aren't picked up).
#   2. chflags nohidden on .venv/.../*.pth — uv on macOS sets the `hidden` flag on
#      .pth files, and Python's site.py SKIPS hidden .pth files. Without un-hiding,
#      the editable install is present-but-invisible: `pip show` claims it works
#      but `import clipper` fails outside of CWD-relative invocations.

UV := UV_NO_EDITABLE=0 uv
PTH_DIR := .venv/lib/python3.12/site-packages
UNHIDE := test -d $(PTH_DIR) && chflags nohidden $(PTH_DIR)/*.pth 2>/dev/null || true

.PHONY: sync test lint mcp-stdio clean

sync:
	$(UV) sync
	@$(UNHIDE)

test: sync
	$(UV) run pytest -q

lint:
	$(UV) run ruff check .

mcp-stdio: sync
	$(UV) run clipper mcp-stdio

clean:
	rm -rf .venv .pytest_cache **/__pycache__
