# ─────────────────────────────────────────────────────────────────────────
# android-media-player — homelab app Makefile (single service: the update /
# monitor server, a stdlib http.server on :9742, network_mode host).
# `make help`. See the homelab-app-standard skill.
# ─────────────────────────────────────────────────────────────────────────

# ==== CONFIG ===============================================================
APP     := android-media-player
# Image compose builds (project-service, no tag).
IMAGES  := android-media-player-update-server
SVC     := update-server
BE_PORT := 9742
# ==========================================================================

VERSION := $(shell cat VERSION 2>/dev/null || echo 0.0.0)
GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo nogit)

.PHONY: help up up-build down build build-only tag-images pull restart logs ps status \
	clean health version push release

help:
	@echo "$(APP) — v$(VERSION) ($(GIT_SHA))"
	@echo ""
	@echo "  lifecycle:  up  up-build  down  build  pull  restart  logs  ps  status  clean"
	@echo "  ops:        health  version"
	@echo "  release:    push (all remotes)   release V=X.Y.Z (bump+tag+build+push)"

# ─── lifecycle ────────────────────────────────────────────────────────────
up:            ; docker compose up -d
up-build:                                     ## rebuild + start, then tag images
	docker compose up -d --build
	@$(MAKE) tag-images
down:          ; docker compose down
# Compile WITHOUT stamping tags. `release` uses this: it builds before it
# commits (fail-fast), so at build time GIT_SHA is still the PREVIOUS commit
# and stamping :<sha> there would name the wrong commit — and clobber the
# previous release's tag. release stamps once, after the commit, via
# tag-images. Plain `make build` still builds AND tags.
build-only:
	docker compose build
build:                                        ## build image, then tag it
	docker compose build
	@$(MAKE) tag-images
tag-images:
	@for img in $(IMAGES); do \
	  docker image inspect $$img:latest >/dev/null 2>&1 || { echo "no $$img:latest — run 'make build'"; exit 1; }; \
	  docker tag $$img:latest $$img:$(VERSION); \
	  docker tag $$img:latest $$img:$(GIT_SHA); \
	  echo "$$img -> latest, $(VERSION), $(GIT_SHA)"; \
	done
pull:          ; docker compose pull && docker compose up -d
restart:       ; docker compose restart $(SVC)
logs:          ; docker compose logs -f $(SVC)
ps:            ; docker compose ps
status:        ; docker compose ps
clean:         ; docker compose down --remove-orphans

# ─── ops ──────────────────────────────────────────────────────────────────────
# network_mode: host, so :9742 is on the host.
health:
	@curl -fsS localhost:$(BE_PORT)/healthz | python3 -m json.tool
version:       ; @echo "$(VERSION) ($(GIT_SHA))"

# ─── release / push ────────────────────────────────────────────────────────────
push:
	@remotes="$$(git remote)"; \
	 [ -n "$$remotes" ] || { echo "no git remotes configured"; exit 1; }; \
	 echo "$$remotes" | grep -qE '^(origin|nfsrbr1)$$' || { echo "refusing: no canonical remote (origin/nfsrbr1)"; exit 1; }; \
	 for r in $$remotes; do echo ">> pushing to $$r"; git push $$r HEAD --follow-tags || exit 1; done
# Build FIRST (fail-fast). VERSION is the single source; update CHANGELOG first.
release:
	@if [ -z "$(V)" ]; then echo "usage: make release V=X.Y.Z"; exit 1; fi
	@echo "$(V)" > VERSION
	@$(MAKE) build-only
	@git add VERSION CHANGELOG.md
	@git commit -m "release: v$(V)"
	@$(MAKE) tag-images
	@git tag -a "v$(V)" -m "v$(V)"
	@$(MAKE) push
