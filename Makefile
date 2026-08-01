.PHONY: install uninstall

PREFIX ?= /usr/local

install:
	@echo "Installing claude-code-api-watchdog to $(PREFIX)/bin..."
	@install -d $(PREFIX)/bin
	@install -m 755 bin/claude-code-api-watchdog $(PREFIX)/bin/claude-code-api-watchdog
	@echo "Done. Run 'claude-code-api-watchdog --help' to verify."

uninstall:
	@echo "Removing claude-code-api-watchdog from $(PREFIX)/bin..."
	@rm -f $(PREFIX)/bin/claude-code-api-watchdog
	@echo "Done."
