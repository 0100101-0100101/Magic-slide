import json
import tkinter.messagebox
import os
import sys
import subprocess
import tempfile
import stat
import requests
import shutil

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature


class Updater:
    """
    Self-update flow for a PyInstaller-packaged macOS build, using a raw
    Version.json file in a GitHub repo as the source of truth.

    Supports two packaging styles via `mac_target`:

    mac_target="onefile" (a raw PyInstaller --onefile binary, no icon/bundle):
        updates/Version.json  -> just a bare number, e.g.   1.2
        updates/YourApp-mac   -> the latest macOS build (no extension needed)
        updates/YourApp-mac.sig -> detached RSA signature of that file

        macOS doesn't lock a running executable the way Windows does, but a
        small helper shell script still waits for the process to exit, then
        swaps the file, marks it executable, and clears the Gatekeeper
        "quarantine" attribute macOS attaches to anything downloaded from
        the internet - skip that and the user gets a "Permission denied" or
        "unidentified developer" block on relaunch.

    mac_target="app_bundle" (a proper .app, built with --windowed):
        updates/Version.json            -> just a bare number, e.g.   1.2
        updates/YourApp-mac.app.zip     -> the .app bundle, zipped (raw
                                            GitHub URLs can't serve a
                                            directory directly)
        updates/YourApp-mac.app.zip.sig -> detached RSA signature of that zip

        A .app is a *directory*, not one file, so the whole folder gets
        swapped rather than a single binary. After quitting, the helper
        script deletes the old .app folder, unzips the new one into place,
        recursively clears quarantine (`xattr -cr`, not just `-d`, since it
        has to touch every file inside the bundle), and re-applies an
        ad-hoc code signature (`codesign --force --deep --sign -`) - without
        this last step Gatekeeper refuses to launch a bundle whose signature
        no longer matches its (now-modified) contents. Ad-hoc signing is
        free but still shows an "unidentified developer" warning on first
        launch; removing that warning entirely requires a paid Apple
        Developer account and full notarization, which is a separate,
        larger setup.

    Unlike a plain checksum (which only catches a corrupted/tampered-in-transit
    download), this verifies a cryptographic *signature* against a public key
    that is embedded in this source file - so it ships baked into your built
    app. Only whoever holds the matching PRIVATE key (kept offline, never
    committed to the repo) can produce a signature this code will accept.
    Even someone with push access to the GitHub repo can't forge a valid
    signature without that private key.

    One-time setup (do this on your own machine, keep the private key safe):
        openssl genpkey -algorithm RSA -out private_key.pem -pkeyopt rsa_keygen_bits:2048
        openssl rsa -pubout -in private_key.pem -out public_key.pem
    Paste the contents of public_key.pem into PUBLIC_KEY_PEM below.

    Every time you publish a new build:
        openssl dgst -sha256 -sign private_key.pem -out YourApp-mac.sig YourApp-mac
        # or, for app_bundle mode:
        ditto -c -k --sequesterRsrc --keepParent YourApp.app YourApp-mac.app.zip
        openssl dgst -sha256 -sign private_key.pem -out YourApp-mac.app.zip.sig YourApp-mac.app.zip
    Commit the build artifact and its .sig file to the repo. NEVER commit
    private_key.pem anywhere.
    """

    # Paste your own public key here - this is safe to share/commit, it's the
    # private key that must stay secret and offline.
    PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzYSg8jVsczPIVxFcmFxq
+eZdSmuCeMI6sQ2hbPqZTqAMhnjCfRUKxRjYxXkQZJEvlbFy9V/cRAUsy0uRa6Ju
DHtVDuhMeqYWecsvNESV40fUOOhArgwhsTaz8wHPlhzJ4f1Tq7MB0JmHGMfqSWa+
Z87W9y7U6+HhNid47V+m2qv3prP8QLFFNmZdxIy/LsZIVNNdepotpiOw5aZsIgPW
bgH0VyK7zvifVHJU+2n9oJQ3ZWX45XyowVhfnIB6QiRysW5sm10DEkAJWuEvCfq8
GTdZHPM2xcSPkg+t2CUTrE8PFTS2TYGTD6Zn/KGK+H5+qj3J5yrPvKQcIsrhoeiD
owIDAQAB
-----END PUBLIC KEY-----
"""
    # ^ EXAMPLE KEY - generated for demonstration purposes only. Replace this
    # with your own before shipping, or anyone with the matching example
    # private key (which is NOT secret - it was generated in this chat) could
    # sign a malicious update.

    def __init__(
        self,
        current_version: float,
        version_url: str = "https://raw.githubusercontent.com/0100101-0100101/fuzzy-pancake/main/updates/Version.json",
        exe_url: str = None,
        signature_url: str = None,
        app_name: str = "Virtual Mouse",
        mac_target: str = "onefile",  # "onefile" or "app_bundle"
    ):
        self.current_version = current_version
        self.version_url = version_url
        self.app_name = app_name
        self.mac_target = mac_target

        # Pick the right default URLs for the chosen packaging style unless
        # the caller explicitly overrode them. Update these to match
        # whatever you actually name your builds in the repo.
        if exe_url is not None:
            self.exe_url = exe_url
        elif self.mac_target == "app_bundle":
            self.exe_url = "https://raw.githubusercontent.com/0100101-0100101/fuzzy-pancake/main/updates/YourApp-mac.app.zip"
        else:
            self.exe_url = "https://raw.githubusercontent.com/0100101-0100101/fuzzy-pancake/main/updates/YourApp-mac"

        if signature_url is not None:
            self.signature_url = signature_url
        elif self.mac_target == "app_bundle":
            self.signature_url = "https://raw.githubusercontent.com/0100101-0100101/fuzzy-pancake/main/updates/YourApp-mac.app.zip.sig"
        else:
            self.signature_url = "https://raw.githubusercontent.com/0100101-0100101/fuzzy-pancake/main/updates/YourApp-mac.sig"

    def get_latest_version(self):
        """Fetch the latest version number from the raw Version.json file.
        Returns None on any failure so callers can distinguish
        'no update available' from 'couldn't check'."""
        try:
            response = requests.get(self.version_url, timeout=10)
        except requests.RequestException:
            tkinter.messagebox.showerror(
                self.app_name,
                "Could not reach the update server. Check your internet connection.",
            )
            return None

        if response.status_code != 200:
            tkinter.messagebox.showerror(
                self.app_name,
                f"Failed to fetch latest version (HTTP {response.status_code}).",
            )
            return None

        try:
            return float(response.text.strip())
        except ValueError:
            tkinter.messagebox.showerror(
                self.app_name,
                "Update server returned an unexpected version format.",
            )
            return None

    def get_signature(self):
        """Fetch the detached signature bytes for the latest build.
        Returns None on any failure."""
        try:
            response = requests.get(self.signature_url, timeout=10)
        except requests.RequestException:
            return None

        if response.status_code != 200:
            return None

        return response.content

    def verify_signature(self, file_path: str, signature: bytes) -> bool:
        """Verifies that `signature` is a valid RSA/SHA-256 signature of
        the file at `file_path`, produced by the holder of the private key
        matching PUBLIC_KEY_PEM. Returns True only if it checks out."""
        try:
            public_key = serialization.load_pem_public_key(self.PUBLIC_KEY_PEM)
        except (ValueError, TypeError):
            return False

        with open(file_path, "rb") as f:
            file_bytes = f.read()

        # NOTE: this expects a signature produced with PKCS1v15 padding
        # (the default for `openssl dgst -sha256 -sign ...`). If you sign
        # with a different padding scheme, update this to match.
        try:
            public_key.verify(
                signature,
                file_bytes,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return True
        except InvalidSignature:
            return False

    def check_for_update(self):
        """Checks for an update and, if one exists, asks the user whether
        to install it. Returns True if an update was kicked off, else False."""
        latest = self.get_latest_version()
        if latest is None:
            return False

        if latest <= self.current_version:
            return False

        should_update = tkinter.messagebox.askyesno(
            self.app_name,
            f"A new version is available ({latest}, you have {self.current_version}).\n"
            "Would you like to update now?",
        )
        if not should_update:
            return False

        self.update(latest)
        return True

    def update(self, new_version: float):
        """Downloads the new build, verifies its signature, then hands off
        to a shell helper that swaps it in after this process exits and
        relaunches it. The swap mechanism depends on `mac_target`."""
        current_exe = sys.executable  # path to the running binary (when frozen)
        app_dir = os.path.dirname(current_exe)

        # 1. Download the new build to a temp file first, so a failed/partial
        #    download never corrupts the currently-working install.
        try:
            with requests.get(self.exe_url, stream=True, timeout=30) as r:
                if r.status_code != 200:
                    tkinter.messagebox.showerror(
                        self.app_name,
                        f"Update download failed (HTTP {r.status_code}).",
                    )
                    return False

                suffix = ".zip" if self.mac_target == "app_bundle" else ""
                tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
                with os.fdopen(tmp_fd, "wb") as f:
                    shutil.copyfileobj(r.raw, f)
        except requests.RequestException as e:
            tkinter.messagebox.showerror(self.app_name, f"Update download failed: {e}")
            return False

        # 2. Verify the download's authenticity before doing anything else
        #    with it. This is what stops a tampered/MITM'd download - or even
        #    a malicious file pushed by someone with repo write access - from
        #    ever being installed. Only a file signed by the real private key
        #    holder will pass.
        signature = self.get_signature()
        if signature is None:
            os.remove(tmp_path)
            tkinter.messagebox.showerror(
                self.app_name,
                "Could not verify the update's authenticity (signature unavailable). "
                "Update cancelled for your safety.",
            )
            return False

        if not self.verify_signature(tmp_path, signature):
            os.remove(tmp_path)
            tkinter.messagebox.showerror(
                self.app_name,
                "The downloaded update failed signature verification and will not "
                "be installed. This could mean the download was tampered with. "
                "Please try again later, and consider reporting this.",
            )
            return False

        # 3. Write the new version number now, so if something goes wrong
        #    mid-swap the user at least knows what they *should* be on
        #    (optional — remove if you'd rather bump it after a verified relaunch).
        try:
            with open(os.path.join(app_dir, "Version.json"), "w") as f:
                json.dump(new_version, f)
        except OSError:
            pass  # non-fatal, just means the version check will nag again next launch

        # 4. Hand off to the packaging-style-specific swap+relaunch mechanism.
        if self.mac_target == "app_bundle":
            self._apply_app_bundle_update(current_exe, tmp_path)
        else:
            self._apply_onefile_update(current_exe, tmp_path)

        sys.exit(0)  # unreachable if the helper already relaunched us, but just in case

    def _apply_onefile_update(self, current_exe: str, tmp_path: str):
        """Writes and launches a shell helper that waits for this process
        (by PID) to exit, then swaps the file in, marks it executable,
        clears the Gatekeeper quarantine flag that macOS attaches to
        anything downloaded from the internet, and relaunches it.

        Assumes `current_exe` is a standalone PyInstaller --onefile binary,
        not a .app bundle.
        """
        my_pid = os.getpid()
        sh_path = os.path.join(tempfile.gettempdir(), "apply_update.sh")
        sh_contents = f"""#!/bin/bash
while kill -0 {my_pid} 2>/dev/null; do
    sleep 0.5
done
mv -f "{tmp_path}" "{current_exe}"
chmod +x "{current_exe}"
xattr -d com.apple.quarantine "{current_exe}" 2>/dev/null
open -n "{current_exe}"
rm -- "$0"
"""
        with open(sh_path, "w") as f:
            f.write(sh_contents)

        # Make the helper script itself executable before running it.
        st = os.stat(sh_path)
        os.chmod(sh_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        subprocess.Popen(
            ["/bin/bash", sh_path],
            start_new_session=True,  # detach from this process so it survives our exit
        )

    def _apply_app_bundle_update(self, current_exe: str, tmp_zip_path: str):
        """Writes and launches a shell helper for .app-bundle deployments.
        A .app is a directory, so this replaces the whole bundle rather
        than a single file: waits for the process to exit, deletes the old
        bundle, unzips the new one into place, recursively clears the
        Gatekeeper quarantine flag on every file inside it (a plain
        `xattr -d` only touches one file - bundles need `-cr`, recursive),
        re-applies an ad-hoc code signature (required, since modifying a
        signed bundle's contents invalidates its existing signature and
        Gatekeeper will refuse to launch a bundle whose signature doesn't
        match its contents), then relaunches it.

        `current_exe` is expected to be the path to the binary INSIDE the
        bundle (i.e. `sys.executable`, which for a --windowed PyInstaller
        build resolves to YourApp.app/Contents/MacOS/YourApp) - this method
        walks up two directories from there to find the .app root.
        """
        my_pid = os.getpid()

        # Contents/MacOS/YourApp -> Contents/MacOS -> Contents -> YourApp.app
        bundle_path = os.path.abspath(
            os.path.join(os.path.dirname(current_exe), "..", "..")
        )
        if not bundle_path.endswith(".app"):
            tkinter.messagebox.showerror(
                self.app_name,
                "Could not locate the .app bundle to update. "
                "This build may not be packaged as expected.",
            )
            os.remove(tmp_zip_path)
            return

        apps_dir = os.path.dirname(bundle_path)  # e.g. /Applications
        sh_path = os.path.join(tempfile.gettempdir(), "apply_update.sh")
        sh_contents = f"""#!/bin/bash
while kill -0 {my_pid} 2>/dev/null; do
    sleep 0.5
done
rm -rf "{bundle_path}"
unzip -o -q "{tmp_zip_path}" -d "{apps_dir}"
xattr -cr "{bundle_path}"
codesign --force --deep --sign - "{bundle_path}"
open -n "{bundle_path}"
rm -f "{tmp_zip_path}"
rm -- "$0"
"""
        with open(sh_path, "w") as f:
            f.write(sh_contents)

        st = os.stat(sh_path)
        os.chmod(sh_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        subprocess.Popen(
            ["/bin/bash", sh_path],
            start_new_session=True,
        )


class Config:
    def __init__(self):
        pass

    def load_config(self, setting_list=[]):
        with open("config.json", "r") as f:
            setting_list = json.load(f)
            return setting_list

    def save_config(self, setting_list=[]):
        with open("config.json", "w") as f:
            json.dump(setting_list, f, indent=4)


class Builder:
    def __init__(self):
        pass

    def check_for_first_boot(self):
        return not os.path.exists("Version.json")

    def run_setup(self, baked_version=0.1, setting_list={"user": 1, "sensitivity": 280}):
        with open("Version.json", "x") as f:
            json.dump(baked_version, f)
        with open("config.json", "x") as f:
            json.dump(setting_list, f, indent=4)


if __name__ == "__main__":
    # Example usage on app startup:
    builder = Builder()
    if builder.check_for_first_boot():
        builder.run_setup()

    with open("Version.json", "r") as f:
        current_version = json.load(f)

    # Pass mac_target="app_bundle" here if you're packaging with --windowed.
    updater = Updater(current_version=current_version, mac_target="onefile")
    updater.check_for_update()