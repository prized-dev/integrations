# Patches for other projects (not opened yet)

Prized as a sandbox provider inside two frameworks that eval and RL teams already use. Each folder
holds one commit as a `git format-patch` file and the text for its pull request. Nothing here has
been sent upstream: opening a pull request on another project's repository is a person's step.

| Folder | Project | Applies on | Status |
| --- | --- | --- | --- |
| `harbor/` | [harbor-framework/harbor](https://github.com/harbor-framework/harbor): a built-in environment, `-e prized` | `main` at 5d5e58a2 (2026-11-10) | Passes Harbor's own checks in a clean VM. Ran Terminal-Bench 2.1's reference solutions live on 2026-11-09: 81 of 89 with reruns. |
| `miles/` | [radixark/miles](https://github.com/radixark/miles): a provider for the OpenEnv route (one paused sandbox per task, forked per episode) | `main` at 171c33bd8f (2026-11-09) | Offline tests pass. **Not runnable yet**: it builds task images with Docker in the sandbox, and the Docker template is not available. No real task has run through it. |

## Apply one

```sh
git clone https://github.com/harbor-framework/harbor && cd harbor
git checkout -b prized-environment 5d5e58a2
git am /path/to/upstream-patches/harbor/prized-environment.patch
```

```sh
git clone https://github.com/radixark/miles && cd miles
git checkout -b prized-sandbox-provider 171c33bd8f
git am /path/to/upstream-patches/miles/prized-provider.patch
```

Each `PR.md` says what the patch adds, how it was tested, what was not run, and its limits.

The Harbor patch is the in-tree form of [`harbor/`](../harbor) in this repository (native mode:
a task's prebuilt image, no Docker). The plugin there stays the way to use Prized with a released
Harbor.
