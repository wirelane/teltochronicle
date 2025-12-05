# ------------------------------------------------------------
# Teltochronicle Makefile
# Convenience targets to update metadata, SDK archives,
# submodule histories, and push everything.
# ------------------------------------------------------------

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
submodule-push:
	git submodule foreach 'git push --all'
	git submodule foreach 'git push --tags'

# Push main repository
push:
	git push 

#Pull all upstream changes for main repo + LFS + submodules.
pull:
	@echo "Pulling main repository..."
	git pull

	@echo "Updating git LFS objects..."
	git lfs fetch
	git lfs pull

	@echo "Updating submodules..."
	git submodule update --init --recursive

	@echo "Fetching upstream commits in submodules..."
	git submodule foreach 'git pull'

	@echo "Upstream sync complete."


# Full automated pipeline: update everything & push
all: update stage commit submodule-push push
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

.PHONY: update stage commit submodule-push push all prepare pull add-model