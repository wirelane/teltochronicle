# Teltochronicle
A changelog and archival tool for the firmware GPL SDKs of Teltonika routers.

Teltochronicle retrieves firmware metadata and changelogs from the official Teltonika wiki, downloads and archives GPL SDK tarballs, and builds per-model Git histories from the extracted SDK contents. These histories allow detailed inspection of differences between SDK releases across firmware versions.  

Each model maintains:
- Markdown changelog
- Firmware metadata in JSON
- directory of downloaded SDK archives (managed with git-lfs)
- Git submodule containing the SDK history

The dates of the commits in each submodule match the release dates of the respective firmware version in order to keep a feeling
for the relation between file updates/changes and their age.

All processing is performed by the `teltochronicle.py` tool.

## Firmware Changelog Links

### RUT951
- [Firmware Changelog](models/RUT951/RUT951.md)
- Repo: see submodule in `models/RUT951/repo`
- [SDKs](models/RUT951/sdks)
- [JSON](models/RUT951/RUT951.json)

### RUT950
- [Firmware Changelog](models/RUT950/RUT950.md)
- Repo: see submodule in `models/RUT950/repo`
- [SDKs](models/RUT950/sdks)
- [JSON](models/RUT950/RUT950.json)

### RUTX09
- [Firmware Changelog](models/RUTX09/RUTX09.md)
- Repo: see submodule in `models/RUTX09/repo`
- [SDKs](models/RUTX09/sdks)
- [JSON](models/RUTX09/RUTX09.json)

## Pulling from the Upstream Repository (with Git LFS and Submodules)

> Make sure that you have Git LFS installed and set up with `git lfs install`.

To ensure that SDK archives and history submodules are correctly retrieved, run:

```
make pull
```

## Updating the Repository

To regenerate firmware metadata, changelogs, SDK history and also commit and push everything, run:

```
make all
```

If you want to review before pushing everything, run instead:

```
make prepare
```


## Adding a New Model

Add the model to the configuration in `teltochronicle.py`:

```
MODEL_CONFIG = {
    ...
    "<MODEL>": "<PRODUCT CODE>",
}
```

Run the following command to create all artificats for the new model as well as adding the origin
to the repository and the submodule including pushing all changes to the given remote:
```
make add-model MODEL=<MODEL> REPO_URL=<REPO URL>
```