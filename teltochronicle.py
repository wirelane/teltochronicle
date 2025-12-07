#!/usr/bin/env python3
import os
import re
import sys
import json
import shutil
import tarfile
import tempfile
import subprocess
import urllib.request
from urllib.error import HTTPError, URLError
from html.parser import HTMLParser
from datetime import datetime


# =====================================================================
# Config
# =====================================================================

BASE_SDK_URL = "https://firmware.teltonika-networks.com"

# model_name -> product_code
MODEL_CONFIG = {
    "RUT951": "RUT9M",
    "RUT950": "RUT9",
    "RUTX09": "RUTX",
    # Add more here as needed
}

def get_base_wiki_url(model: str):
    return f"https://wiki.teltonika-networks.com/view/{model.upper()}_Firmware_Downloads"

# =====================================================================
# Extract <div id="mw-content-text">...</div>
# =====================================================================

class PageContainerExtractor(HTMLParser):
    """Extract only the HTML inside <div id="mw-content-text">...</div>."""

    def __init__(self):
        super().__init__()
        self.recording = False
        self.depth = 0
        self.output = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag == "div":
            element_id = attrs.get("id", "")
            if not self.recording and element_id == "mw-content-text":
                self.recording = True
                self.depth = 1
                self.output.append(self.get_starttag_text())
                return

        if self.recording:
            self.output.append(self.get_starttag_text())
            if tag == "div":
                self.depth += 1

    def handle_endtag(self, tag):
        if self.recording:
            self.output.append(f"</{tag}>")
            if tag == "div":
                self.depth -= 1
                if self.depth == 0:
                    self.recording = False

    def handle_data(self, data):
        if self.recording:
            self.output.append(data)

    def get_content(self):
        return "".join(self.output)


def extract_main_container(html: str) -> str:
    parser = PageContainerExtractor()
    parser.feed(html)
    return parser.get_content()


# =====================================================================
# Parse "stable" / "latest" firmware from the table
# =====================================================================

class StableLatestParser(HTMLParser):
    """
    Very small parser to extract stable and latest
    firmware version from the info table on the page.

    We look for <tr><td>FILENAME...</td><td>Stable FW</td></tr>
    and same for "Latest FW".

    Only filenames starting with the given product_code are considered.
    """

    def __init__(self, product_code: str):
        super().__init__()
        self.product_code = product_code
        self.in_tr = False
        self.in_td = False
        self.current_cells = []
        self.current_text = ""
        self.versions = {}

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.in_tr = True
            self.current_cells = []
        elif tag == "td" and self.in_tr:
            self.in_td = True
            self.current_text = ""

    def handle_endtag(self, tag):
        if tag == "td" and self.in_td:
            self.current_cells.append(self.current_text.strip())
            self.in_td = False
        elif tag == "tr" and self.in_tr:
            self._process_row()
            self.in_tr = False

    def handle_data(self, data):
        if self.in_td:
            self.current_text += data

    def _process_row(self):
        if len(self.current_cells) < 2:
            return
        filename: str = self.current_cells[0].strip()
        description: str = self.current_cells[1].strip()

        if not filename.startswith(f"{self.product_code}_R_"):
            return

        if "Stable FW" in description:
            key = "stable"
        elif "Latest FW" in description:
            key = "latest"
        else:
            return
        
        # Convert filename to firmware version by stripping last "_..."
        # e.g. RUT9M_R_00.07.18.3_WEBUI.bin -> RUT9M_R_00.07.18.3
        fw_version, _ = filename.rsplit("_", 1)
        self.versions[key] = fw_version


# =====================================================================
# Parse firmware headings, warnings, changelog trees
# =====================================================================

class FirmwareTreeParser(HTMLParser):
    """
    Parses:
      - "Changelog" section
      - Firmware headings containing '_R_'
      - Release date from heading text '... | YYYY.MM.DD'
      - Warning text between heading and first <ul>
      - One or more root <ul> changelog blocks
      - Nested <ul>/<li> into a tree

    firmwares: version -> {
      "heading": str,
      "version": str,
      "release_date": str,
      "warning": str | None,
      "tree": [ { "text": str, "children": [...] }, ... ]
    }
    """

    def __init__(self):
        super().__init__()

        self.in_heading = False
        self.heading_buffer = ""
        self.in_changelog_section = False

        self.current_version = None
        self.current_heading_text = ""
        self.current_release_date = ""
        self.current_warning = ""

        self.awaiting_root_ul = False
        self.collecting_warning = False

        self.collecting_changelog = False
        self.changelog_ul_depth = 0
        self.current_root = None
        self.node_stack = []

        self.firmwares = {}

    def _append_to_current_node(self, text: str):
        if self.collecting_changelog and self.node_stack:
            self.node_stack[-1]["text"] += text

    def _start_new_version(self, heading_text: str):
        # "RUT9M_R_00.07.18 | 2025.10.01"
        left = heading_text
        right = ""
        if "|" in heading_text:
            left, right = heading_text.split("|", 1)
            left = left.strip()
            right = right.strip()

        version = None
        for tok in left.replace("|", " ").split():
            if "_R_" in tok:
                version = tok
                break
        if version is None:
            return

        self.current_version = version
        self.current_heading_text = heading_text.strip()
        self.current_release_date = right
        self.current_warning = ""

        self.awaiting_root_ul = True
        self.collecting_warning = True

        self.collecting_changelog = False
        self.changelog_ul_depth = 0
        self.current_root = None
        self.node_stack = []

        self.firmwares[version] = {
            "heading": self.current_heading_text,
            "version": version,
            "release_date": right,
            "warning": "",
            "tree": [],
        }

    def handle_starttag(self, tag, attrs):
        # headings
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.in_heading = True
            self.heading_buffer = ""

        # root <ul> for changelog
        if self.awaiting_root_ul and self.current_version and tag == "ul":
            self.collecting_changelog = True
            self.changelog_ul_depth = 1
            self.awaiting_root_ul = False

            fw = self.firmwares[self.current_version]
            if not fw["tree"]:
                # first root <ul> → finalize warning
                fw["warning"] = self.current_warning.strip()
                self.collecting_warning = False
                self.current_root = []
                fw["tree"] = self.current_root
            else:
                # additional root <ul> → append to same tree
                self.current_root = fw["tree"]

            self.node_stack = []
            return

        if self.collecting_changelog and tag == "ul":
            self.changelog_ul_depth += 1

        if self.collecting_changelog and tag == "li":
            node = {"text": "", "children": []}
            if self.node_stack:
                self.node_stack[-1]["children"].append(node)
            else:
                assert self.current_root is not None
                self.current_root.append(node)
            self.node_stack.append(node)

    def handle_endtag(self, tag):
        # end heading
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            heading_text = self.heading_buffer.strip()

            if "Changelog" in heading_text:
                self.in_changelog_section = True
            elif self.in_changelog_section and "_R_" in heading_text:
                self._start_new_version(heading_text)

            self.in_heading = False

        # end li
        if tag == "li" and self.collecting_changelog and self.node_stack:
            node = self.node_stack[-1]
            node["text"] = node["text"].strip()
            self.node_stack.pop()

        # end ul
        if self.collecting_changelog and tag == "ul":
            self.changelog_ul_depth -= 1
            if self.changelog_ul_depth == 0:
                self.collecting_changelog = False
                self.node_stack = []
                self.awaiting_root_ul = True

    def handle_data(self, data):
        if self.in_heading:
            self.heading_buffer += data
        elif self.collecting_changelog and self.node_stack:
            self._append_to_current_node(data)
        elif self.collecting_warning and self.current_version and not self.collecting_changelog:
            self.current_warning += data


def tree_to_markdown(nodes, heading_level_base=3):
    """Convert changelog tree into nested headings + bullets."""
    lines = []

    def walk(node_list, depth):
        for node in node_list:
            text = node["text"].strip()
            if not text:
                if node["children"]:
                    walk(node["children"], depth + 1)
                continue

            if node["children"]:
                level = heading_level_base + depth
                if level > 6:
                    level = 6
                lines.append(f"{'#' * level} {text}")
                walk(node["children"], depth + 1)
            else:
                lines.append(f"* {text}")

    walk(nodes, depth=0)
    return "\n".join(lines)


# =====================================================================
# Firmware version → SDK path parts (product_code etc.)
# =====================================================================

def derive_sdk_path_parts(version: str):
    """
    'RUT9M_R_00.07.06.20' ->
      product_code : 'RUT9M'
      unified_full : '00.07.06.20'
      unified_short: '7.6.20'
      gpl_version  : 'RUT9M_R_GPL_00.07.06.20'
    """
    parts = version.split("_")
    if len(parts) < 3:
        raise ValueError(f"Unexpected firmware version format: {version}")

    product_code = parts[0]
    unified_full = parts[-1]

    comps = []
    for comp in unified_full.split("."):
        stripped = comp.lstrip("0")
        if stripped:
            comps.append(stripped)
    unified_short = ".".join(comps) if comps else unified_full

    gpl_version = version.replace("_R_", "_R_GPL_", 1)

    return {
        "product_code": product_code,
        "unified_full": unified_full,
        "unified_short": unified_short,
        "gpl_version": gpl_version,
    }


def get_minor_from_unified_short(unified_short: str) -> str:
    # '7.6.20' or '7.6' -> '7.6'
    parts = unified_short.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else parts[0]


def build_sdk_url(base_url: str, unified_short: str, product_code: str, gpl_version: str) -> str:
    # {base}/{unified_short}/{product_code}/{gpl_version}.tar.gz
    return f"{base_url.rstrip('/')}/{unified_short}/{product_code}/{gpl_version}.tar.gz"


# =====================================================================
# Release date sorting (YYYY.MM.DD → datetime.date)
# =====================================================================

def sorted_firmwares_by_date(firmwares: dict):
    """
    Sort firmware entries by release_date (YYYY.MM.DD) ascending.
    Missing/invalid dates fall back to date.min.
    """
    items = []
    for version, fw in firmwares.items():
        ds = (fw["release_date"] or "").strip()
        if ds:
            try:
                dt = datetime.strptime(ds, "%Y.%m.%d")
            except ValueError:
                dt = datetime.min
        else:
            dt = datetime.min
        items.append((dt, version, fw))

    items.sort(key=lambda x: x[0])
    return items


# =====================================================================
# SDK download with 403 persistence
# =====================================================================

def load_unavailable_versions(skip_file: str):
    unavailable = set()
    if not os.path.exists(skip_file):
        return unavailable
    with open(skip_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                unavailable.add(line)
    return unavailable


def download_sdk_if_needed(
    base_url: str,
    version: str,
    dest_root: str,
    skip_file: str,
    unavailable_versions: set,
):
    if version in unavailable_versions:
        print(f"[SKIP] {version}: Previously marked unavailable (HTTP 403).")
        return

    info = derive_sdk_path_parts(version)
    product_code = info["product_code"]
    unified_short = info["unified_short"]
    gpl_version   = info["gpl_version"]

    if not unified_short:
        print(f"[WARN] {version}: Could not determine short unified firmware version.")
        return

    url = build_sdk_url(base_url, unified_short, product_code, gpl_version)

    os.makedirs(dest_root, exist_ok=True)
    dest_file = os.path.join(dest_root, f"{gpl_version}.tar.gz")

    if os.path.exists(dest_file):
        print(f"[SKIP] {version}: SDK already downloaded.")
        return

    print(f"[INFO] {version}: Downloading SDK from {url}")

    try:
        with urllib.request.urlopen(url) as resp, open(dest_file, "wb") as f:
            f.write(resp.read())
        print(f"[OK]   {version}: Downloaded successfully.")

    except HTTPError as e:
        if e.code == 403:
            print(f"[WARN] {version}: SDK unavailable on server (HTTP 403). Marking permanently unavailable.")
            unavailable_versions.add(version)
            with open(skip_file, "a", encoding="utf-8") as f:
                f.write(version + "\n")
        else:
            print(f"[ERROR] {version}: HTTP error {e.code} ({e.reason}).")
        if os.path.exists(dest_file):
            os.remove(dest_file)

    except URLError as e:
        print(f"[ERROR] {version}: URL error ({e.reason}).")
        if os.path.exists(dest_file):
            os.remove(dest_file)


# =====================================================================
# Git helpers (per repo)
# =====================================================================

def is_git_working_tree_root(path: str) -> bool:
    """
    Return True if 'path' is a valid Git repository root.

    One needs to be careful here to avoid an edge case when working
    with nested, uninitialized repositories, e.g. submodules that have not
    been checked out yet, since we are working inside a working tree then, but
    not the working tree of the submodule...
    """
    if not os.path.isdir(path):
        return False

    # We intentionally suppress stderr to avoid noise.
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False
    )

    working_tree_root = result.stdout.strip()

    if result.returncode != 0 or len(working_tree_root) == 0:
        # not a git repository or something else broken
        return False

    # account for symlinks or other weirdness
    real_working_tree_root = os.path.realpath(working_tree_root)
    real_path = os.path.realpath(path)

    return real_working_tree_root == real_path


def is_git_submodule(path: str, repo_root: str) -> bool:
    realpath = os.path.realpath(path)

    # We intentionally suppress stderr to avoid noise.
    result = subprocess.run(
        ["git", "submodule", "status", path],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False
    )

    if result.returncode != 0:
        return False

    realpath_submodules = [
        os.path.realpath(line.strip().split(' ')[1])
            for line in result.stdout.splitlines()
    ]

    return realpath in realpath_submodules


def get_current_branch(repo_root: str) -> str | None:
    """
    Return the current Git branch name for the repository at 'path'.
    Returns None if the repo is in a detached HEAD state or if the
    branch cannot be determined.

    Uses: git rev-parse --abbrev-ref HEAD
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True
        )

        branch = result.stdout.strip()

        # Detached HEAD state returns literally: "HEAD"
        if branch == "HEAD" or not branch:
            return None

        return branch

    except Exception:
        return None


def repo_has_tag(repo_path: str, tag: str) -> bool:
    result = subprocess.run(
        ["git", "tag", "--list", tag],
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return tag in (line.strip() for line in result.stdout.splitlines())


def branch_exists(repo_path: str, branch: str) -> bool:
    # check for a local branch first
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode == 0:
        return True
    
    # but we are in doubt also fine with a remote branch
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/remotes/origin/{branch}"],
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.returncode == 0


def checkout_orphan_branch(repo_path: str, branch: str):
    subprocess.run(["git", "checkout", "--orphan", branch], cwd=repo_path, check=True)


def checkout_branch(repo_path: str, branch: str):
    subprocess.run(["git", "checkout", branch], cwd=repo_path, check=True)


def clear_repo_worktree(repo_path: str):
    for name in os.listdir(repo_path):
        if name == ".git":
            continue
        path = os.path.join(repo_path, name)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)


def extract_sdk_into_repo(repo_path: str, sdk_path: str) -> bool:
    """
    Strict: SDK archive must have exactly one root directory and nothing else.
    That directory's contents go into repo_root.
    """
    tmpdir = tempfile.mkdtemp(prefix="sdk_extract_")
    try:
        with tarfile.open(sdk_path, "r:gz") as tar:
            tar.extractall(tmpdir, filter='tar')

        entries = os.listdir(tmpdir)
        if len(entries) != 1:
            print(f"[WARN] SDK archive '{sdk_path}' does not contain exactly one root directory. Skipping import.")
            return False

        root_entry = os.path.join(tmpdir, entries[0])
        if not os.path.isdir(root_entry):
            print(f"[WARN] SDK archive '{sdk_path}' root is not a directory. Skipping import.")
            return False

        for name in os.listdir(root_entry):
            shutil.move(os.path.join(root_entry, name), os.path.join(repo_path, name))

        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def commit_and_tag_repo(repo_path: str, gpl_version: str, release_date: datetime) -> bool:
    subprocess.run(["git", "add", "-A", "."], cwd=repo_path, check=True)

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path,
        stdout=subprocess.PIPE,
        text=True,
    )
    if not status.stdout.strip():
        print(f"[SKIP] {gpl_version}: No changes to commit.")
        return False

    # force git to set commit and author date to the release date
    git_env = os.environ.copy()
    git_env["GIT_AUTHOR_DATE"] = release_date.isoformat()
    git_env["GIT_COMMITTER_DATE"] = git_env["GIT_AUTHOR_DATE"]

    subprocess.run(["git", "commit", "-m", f"SDK {gpl_version}"], cwd=repo_path,
                    check=True, env=git_env)
    subprocess.run(["git", "tag", gpl_version], cwd=repo_path, check=True, env=git_env)
    print(f"[OK]   {gpl_version}: Committed and tagged.")
    return True


def is_lfs_pointer_file(path: str) -> bool:
    """
    Return True if the file at 'path' is a Git LFS pointer file.
    A valid LFS pointer file looks like:

        version https://git-lfs.github.com/spec/v1
        oid sha256:<SHA256>
        size <size>

    Detection is based on this exact format.
    """

    # Pointer files are tiny; skip large files without reading
    try:
        if os.path.getsize(path) > 1024:  # pointer files are usually <200 bytes
            return False
    except OSError:
        return False

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return False

    if len(lines) < 2:
        return False

    # Check required fields
    if lines[0].strip() != "version https://git-lfs.github.com/spec/v1":
        return False

    has_oid = any(line.startswith("oid sha256:") for line in lines)
    has_size = any(line.startswith("size ") for line in lines)

    return has_oid and has_size

import subprocess

def resolve_lfs_pointer(repo_root: str, file_path: str) -> bool:
    """
    Replace a Git LFS pointer file with its actual binary content using:
        git lfs pull --include=<relative-path-to-repo-root>
    Returns True on success, False on failure.
    """
    try:
        subprocess.run(
            ["git", "lfs", "pull", "--include", file_path],
            cwd=repo_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return True

    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Failed to pull LFS object for {file_path}")
        print(e.stderr or e.stdout)
        return False

# =====================================================================
# Branch selection (vX.Y) + master/stable per repo
# =====================================================================

_branch_re = re.compile(r"^v([0-9]+)\.([0-9]+)$")

def get_sorted_minor_branches(repo_root: str):
    result = subprocess.run(
        ["git", "branch", "--format", "%(refname:short)"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    branches = []
    for line in result.stdout.splitlines():
        name = line.strip()
        if _branch_re.match(name):
            branches.append(name)

    def key(branch: str):
        m = _branch_re.match(branch)
        assert m is not None
        return (int(m.group(1)), int(m.group(2)))

    branches.sort(key=key)
    return branches


def reset_branch_to_start_point(repo_root: str, branch: str, start_point: str):
    subprocess.run(["git", "branch", "-f", branch, start_point], cwd=repo_root, check=True)
    print(f"[OK]   {branch} → {start_point}")


def update_branch_head_to_version(repo_root: str, branch: str, version: str):

    info = derive_sdk_path_parts(version)
    if info is None:
        raise ValueError('malformed firmware version string')
    
    tag = info['gpl_version']
    assert repo_has_tag(repo_root, tag)
    
    reset_branch_to_start_point(repo_root, branch, tag)


def create_or_checkout_minor_branch(repo_root: str, minor: str) -> str:
    """
    - If v<minor> exists: checkout it.
    - Else:
        - If we have v<*> branches: base new v<minor> on the newest of them.
        - If none: create v<minor> as orphan.
    """
    branch_name = f"v{minor}"

    if branch_exists(repo_root, branch_name):
        if get_current_branch(repo_root) != branch_name:
            checkout_branch(repo_root, branch_name)
        return branch_name

    branches = get_sorted_minor_branches(repo_root)
    if branches:
        base = branches[-1]
        print(f"[INFO] Creating new branch {branch_name} from {base}.")
        subprocess.run(["git", "checkout", base], cwd=repo_root, check=True)
        subprocess.run(["git", "checkout", "-b", branch_name], cwd=repo_root, check=True)
    else:
        print(f"[INFO] Creating new orphan branch {branch_name} (no previous minor branch found).")
        checkout_orphan_branch(repo_root, branch_name)

    return branch_name


# =====================================================================
# Import SDKs into git (sorted by date) for one model
# =====================================================================
def import_sdks_into_git(firmwares: dict, stable_latest: dict, sdks_root: str, repo_root: str):

    for release_date, version, data in sorted_firmwares_by_date(firmwares):
        warning = data["warning"].strip()
        if warning:
            print(f"[SKIP] {version}: Firmware withdrawn or invalid → not importing into git.")
            continue

        try:
            info = derive_sdk_path_parts(version)
        except ValueError as e:
            print(f"[WARN] {version}: {e}")
            continue

        unified_short = info["unified_short"]
        gpl_version   = info["gpl_version"]

        if not unified_short:
            print(f"[WARN] {version}: Could not determine short unified firmware version.")
            continue
        
        sdk_path = os.path.join(sdks_root, f"{gpl_version}.tar.gz")
        if not os.path.exists(sdk_path):
            print(f"[SKIP] {version}: SDK file not found ({sdk_path}).")
            continue

        if repo_has_tag(repo_root, gpl_version):
            print(f"[SKIP] {version}: Git tag '{gpl_version}' already exists.")
            continue

        if is_lfs_pointer_file(sdk_path):
            print(f"[INFO] {sdk_path}: not yet downloaded from Git LFS -> downloading.")
            # we're working inside the main repository here, not the SDK submodule repository
            if not resolve_lfs_pointer('.', sdk_path):
                continue

        minor = get_minor_from_unified_short(unified_short)
        minor_branch = create_or_checkout_minor_branch(repo_root, minor)

        print(f"[INFO] {version}: Importing SDK {gpl_version} into branch {minor_branch}.")

        clear_repo_worktree(repo_root)
        if not extract_sdk_into_repo(repo_root, sdk_path):
            print(f"[SKIP] {gpl_version}: SDK archive layout invalid → not imported into git.")
            continue

        if not commit_and_tag_repo(repo_root, gpl_version, release_date):
            continue

    latest_fw = stable_latest.get('latest')
    stable_fw = stable_latest.get('stable')

    assert latest_fw is not None and stable_fw is not None

    update_branch_head_to_version(repo_root, 'master', latest_fw)
    update_branch_head_to_version(repo_root, 'stable', stable_fw)


# =====================================================================
# Per-model processing
# =====================================================================

def fetch(url: str) -> str:
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode(errors="ignore")


def process_model(model: str, product_code: str):
    print(f"\n==== Processing model {model} (product_code={product_code}) ====")

    main_repo_root = "."
    model_dir = os.path.join("models", model)
    sdks_root = os.path.join(model_dir, "sdks")
    repo_root = os.path.join(model_dir, "repo")
    unavailable_file = os.path.join(sdks_root, "unavailable_sdks.txt")
    markdown_path = os.path.join(model_dir, f"{model}.md")
    json_path = os.path.join(model_dir, f"{model}.json")

    os.makedirs(model_dir, exist_ok=True)
    os.makedirs(sdks_root, exist_ok=True)
    os.makedirs(repo_root, exist_ok=True)

    wiki_url = get_base_wiki_url(model)

    # 1) Fetch and parse wiki
    html = fetch(wiki_url)
    cleaned = extract_main_container(html)

    # 2) Parse stable/latest firmware from table
    sl_parser = StableLatestParser(product_code)
    sl_parser.feed(cleaned)

    stable_latest = sl_parser.versions

    # 3) Parse changelog
    parser = FirmwareTreeParser()
    parser.feed(cleaned)

    firmwares = parser.firmwares

    # 4) Write markdown changelog for this model
    with open(markdown_path, "w", encoding="utf-8") as md_out:
        md_out.write(f"# {model} firmware changelog\n\n")

        if stable_latest["stable"] or stable_latest["latest"]:
            md_out.write("## Overview\n\n")
            if stable_latest["stable"]:
                md_out.write(f"- Stable firmware: `{stable_latest['stable']}`\n")
            if stable_latest["latest"]:
                md_out.write(f"- Latest firmware: `{stable_latest['latest']}`\n")
            md_out.write("\n---\n\n")
        
        for version, fw in firmwares.items():
            heading_line = fw["heading"].strip() or version
            md_out.write(f"## {heading_line}\n\n")
            warning = fw["warning"].strip()
            if warning:
                md_out.write("> ⚠️\n" f"> {warning}\n\n")
            md = tree_to_markdown(fw["tree"], heading_level_base=3)
            if md:
                md_out.write(md)
                md_out.write('\n\n')
                md_out.write('---')
                md_out.write("\n\n")

    # 5) Write JSON metadata for this model
    meta_list = []
    for version, fw in firmwares.items():
        try:
            info = derive_sdk_path_parts(version)
        except ValueError:
            continue
        # Only keep entries for this product_code
        if info["product_code"] != product_code:
            continue
        entry = {
            "version": version,
            "gpl_version": info["gpl_version"],
            "unified_full": info["unified_full"],
            "unified_short": info["unified_short"],
            "release_date": fw["release_date"].strip(),
            "warning": (fw["warning"].strip() or None),
        }
        meta_list.append(entry)

    json_data = {
        "model": model,
        "product_code": product_code,
        "stable_firmware": stable_latest["stable"],
        "latest_firmware": stable_latest["latest"],
        "firmwares": meta_list,
    }

    with open(json_path, "w", encoding="utf-8") as jf:
        json.dump(json_data, jf, indent=2, ensure_ascii=False)

    # 6) Download SDKs
    unavailable_versions = load_unavailable_versions(unavailable_file)
    for version, fw in firmwares.items():
        if fw["warning"].strip():
            print(f"[SKIP] {version}: Firmware withdrawn or invalid → not downloading SDK.")
            continue
        download_sdk_if_needed(
            BASE_SDK_URL,
            version,
            sdks_root,
            unavailable_file,
            unavailable_versions,
        )

    # 7) Import SDKs to model-specific git repo
    if not is_git_working_tree_root(repo_root):
        if not is_git_submodule(repo_root, main_repo_root):
            subprocess.run(["git", "init"], cwd=repo_root, check=True)
        else:
            print(f"[ERROR] {version}: Submodule {repo_root} has not been checked out yet.")
            return

    import_sdks_into_git(firmwares, stable_latest, sdks_root, repo_root)


def main() -> int:

    if len(sys.argv) not in [1, 2]:
        print("[ERROR] must have exactly one argument with the model to process or none.")
        return 1

    # allow only to process model given as first argument on the command line.
    if len(sys.argv) == 2:
        model = sys.argv[1]

        if model not in MODEL_CONFIG:
            print("[ERROR] unknown model given as first argument")
            return 1

        process_model(model, MODEL_CONFIG[model])

    # otherwise process all models by default
    else:
        for model, product_code in MODEL_CONFIG.items():
            process_model(model, product_code)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())