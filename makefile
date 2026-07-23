PYSRC = $(shell find reterminal_daemon -name '*.py' -o -name '*.html')
SERVICE = 

all: dist/reterminal-daemon.pex

dist/reterminal-daemon.pex: $(PYSRC) pyproject.toml
	uv sync 
	./scripts/build_pex.sh 

install: dist/reterminal-daemon.pex 
	mkdir -p ~/.config/reterminal-daemon
	cp dist/reterminal-daemon.pex ~/bin
	cp systemd/reterminal-daemon.service ~/.config/systemd/user/
	[ -f systemd/reterminal-daemon.env ] && cp systemd/reterminal-daemon.env ~/.config/reterminal-daemon/reterminal-daemon.env
	systemctl --user daemon-reload
	systemctl --user enable reterminal-daemon.service 

start:
	systemctl --user start reterminal-daemon.service 

stop:
	systemctl --user stop reterminal-daemon.service 

status:
	systemctl --user status reterminal-daemon.service 

log:
	journalctl --user -f --unit reterminal-daemon.service

.PHONY: all install start 
