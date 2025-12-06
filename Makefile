# ------------------------------------------------------------
# Teltochronicle Makefile
# Convenience targets to update metadata, SDK archives,
# submodule histories, and push everything.
# ------------------------------------------------------------

# Beware that GNU Make and git still use sh by default, so make sure
# to remain POSIX-compliant in doubt - as things were not annoying enough already.
# see https://github.com/git/git/blob/f0ef5b6d9bcc258e4cbef93839d1b7465d5212b9/run-command.c#L283

# For Ubuntu this unfortunately still means that sh will get you in contact with dash
# by default - and particularly dash is really something else.

# Fortunately, at least in GNU make, the SHELL environment variable is respected, so
# we can override the SHELL for this Makefile and all sub-instances of make as seen
# below -  if it should become necessary.
#export SHELL := /usr/bin/env bash

# However, due to the fact that we're not really getting around sh/dash here anyways,
# we will finally accept defeat and remain POSIX-compliant here, so in order to
# not cause even more confusion.

PYTHON ?= python3

# Main update target: runs teltochronicle to fetch metadata & SDKs
update:
	$(PYTHON) teltochronicle.py

# Stage all changes in models/ (metadata, markdown, SDKs, submodule pointers)
stage:
	git add models/

# Commit staged changes with a standard commit message
commit:
	git commit -m "Update firmware metadata, SDK archives and submodule state" || echo "Nothing to commit."

# Push all submodules: branches and tags
push-submodules:
	git submodule foreach 'git push --force --all'
	git submodule foreach 'git push --tags'

# Push main repository
push-repo:
	git push

# Pull main repo
pull-repo:
	@echo "Pulling main repository..."
	git pull

# Pull LFS objects
pull-lfs:
	@echo "Updating git LFS objects..."
	git lfs fetch
	git lfs pull

# Pull submodules
pull-submodules:
	@echo "Checking if submodules are clean"
	git submodule foreach '\
	  [ -z "$$(git status --porcelain)" ]' \
	    || { echo "Unclean submodules -> refusing to continue -> consider target reset-submodules"; exit 1; }

	@echo "Updating submodules..."
	git submodule update --init --recursive

	@echo "Fast-forwarding all remote branches in all submodules..."
	git submodule foreach '\
	  for b in $$(git for-each-ref --format="%(refname:short)" refs/heads); do \
	    ( [ "$$b" = "master" ] || [ "$$b" = "stable" ] ) && continue; \
	    git checkout "$$b" || exit 1; \
	    git pull --ff-only || exit 1; \
	  done \
	'

# Aggressively reset all submodules back to the remote state.
reset-submodules:
	git submodule foreach '\
		git fetch origin --prune --prune-tags --tags; \
		git checkout --detach origin/HEAD ; \
		for b in $$(git for-each-ref --format="%(refname:short)" refs/heads); do \
			git branch -D -f $$b; \
		done ; \
		git clean -xfd ; \
	'

# Pull all upstream changes for main repo + LFS + submodules
pull: pull-repo pull-lfs pull-submodules
	@echo "Upstream sync complete."

# Full automated pipeline: update everything & push main repo
all: update stage commit push-submodules push-repo
	@echo "All tasks complete."

# Prepare updates without pushing
prepare: update stage commit
	@echo "Repository prepared; review changes before pushing."

# ------------------------------------------------------------
# Add a new model repository as a submodule
#
# Usage:
#   make add-model MODEL=RUT951 REPO_URL=git@github.com:wirelane/teltonika-gpl-sdk-rut951.git
#
# This will:
#   - Expect the model to exist under models/<MODEL>/ (already created by teltochronicle)
#   - Add the REPO_URL as remote origin to the new repo
#   - Add the new repo as a submodule under models/<MODEL>/repo
# ------------------------------------------------------------
# Variables expected:
#   MODEL    — name of the model (example: RUT951)
#   REPO_URL — git URL to the model's SDK-history repo
#
add-model: update
	@if [ -z "$(MODEL)" ]; then \
		echo "ERROR: MODEL variable not set. Example: make add-model MODEL=RUT951 REPO_URL=..."; exit 1; \
	fi
	@if [ -z "$(REPO_URL)" ]; then \
		echo "ERROR: REPO_URL variable not set. Example: make add-model MODEL=RUT951 REPO_URL=git@github.com:you/rut951-sdk.git"; exit 1; \
	fi

	@MODEL_REPO_PATH="models/$(MODEL)/repo"; \
	echo "Adding remote origin to $(REPO_URL) on $$MODEL_REPO_PATH"; \
	git -C "$$MODEL_REPO_PATH" remote add origin "$(REPO_URL)"; \
	\
	echo "Adding submodule at $$MODEL_REPO_PATH pointing to $(REPO_URL)"; \
	git submodule add "$(REPO_URL)" "$$MODEL_REPO_PATH"; \
	\
	echo "Submodule successfully added for model $(MODEL)."

.PHONY: update stage commit push-submodules push-repo all prepare pull pull-repo pull-lfs pull-submodules reset-submodules add-model
