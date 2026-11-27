"""The one SolidCraft release Gantrel uses. Hashes are the release's own SHA256SUMS.txt lines."""

from __future__ import annotations

VERSION = "0.50.0"
RELEASE = f"https://github.com/tangency-labs/solidcraft/releases/download/v{VERSION}"
SUMS = "SHA256SUMS.txt"
# (system, machine) -> (release file, sha256). fetch unpacks bin/solidcraft-cli from the Linux tarballs;
# the other files are named in doctor's steps for a person to download.
FILES = {
    ("linux", "x86_64"): (f"solidcraft-{VERSION}-linux-x86_64.tar.gz",
                          "b0c63f45fe58ab13c40145132b21fdc309f45e294301f1ba525050aa99575920"),
    ("linux", "aarch64"): (f"solidcraft-{VERSION}-linux-aarch64.tar.gz",
                           "5a5c6e87533f70de2ad0fcc23c7b36f45144ccf9ded77ff0099cbbea39b4c337"),
    ("darwin", "universal"): (f"solidcraft-cli-{VERSION}-macos-universal.zip",
                              "05c16524e1efd1dd74b3b0e7564c6ef92b243e7e7afe79c15717fac4750efcd8"),
    ("windows", "x86_64"): (f"solidcraft-{VERSION}-windows-x64-portable.zip",
                            "f9b5be186e966e839af6b1a9bf32dfd0e0663cace9ba91f091ef8d99893a965a"),
}
