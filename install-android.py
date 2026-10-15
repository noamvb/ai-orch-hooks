#!/usr/bin/env python3
"""Install the Android SDK and Flutter on a Claude Code cloud VM.

Called by install-cloud.py from the environment's setup script (root on
Ubuntu). The setup result is snapshotted only when the whole script finishes
in about five minutes, so this installs the lean set the owner's apps need
(compileSdk 36 and 36.1, build-tools 36, platform-tools, Flutter for ParTake)
and leaves the NDK and CMake to the Android Gradle plugin, which downloads
them on a project's first build once the licences are accepted. The SDK and
Flutter downloads run in parallel. Every failure is reported and skipped:
the hooks must install even when Google's servers do not answer.

It also writes a Gradle init script that sends Maven Central downloads to
Google's mirror (see MAVEN_MIRROR). AIORCH_CLOUD_ANDROID=0 skips it all; ANDROID_ROOT and FLUTTER_ROOT below can be
overridden with AIORCH_ANDROID_ROOT and AIORCH_FLUTTER_ROOT for a test run.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile

ANDROID_ROOT = os.environ.get("AIORCH_ANDROID_ROOT", "/opt/android-sdk")
FLUTTER_ROOT = os.environ.get("AIORCH_FLUTTER_ROOT", "/opt/flutter")
BIN = "/usr/local/bin"
REPO_XML = "https://dl.google.com/android/repository/repository2-3.xml"
TOOLS_ZIP = "https://dl.google.com/android/repository/commandlinetools-linux-%s_latest.zip"
PACKAGES = ["platform-tools", "platforms;android-36", "platforms;android-36.1", "build-tools;36.0.0"]
FLUTTER_VERSION = "3.47.5"  # the Deck's Flutter; ParTake builds with it
STEP_TIMEOUT = 200
# Cloud VMs share egress IPs, and Maven Central answers them with HTTP 429
# (two Gradle builds failed that way, 15 Oct 2026); Google's mirror does not.
GRADLE_INIT = os.path.join(os.path.expanduser("~"), ".gradle", "init.d", "aiorch-maven-mirror.gradle")
MAVEN_MIRROR = """\
// Written by ai-orch-hooks install-android.py. Cloud VMs share egress IPs, and
// Maven Central answers them with HTTP 429. Point every Maven Central
// repository at Google's mirror of it instead.
def central = ['https://repo.maven.apache.org/maven2', 'https://repo1.maven.org/maven2']
def mirror = 'https://maven-central.storage-download.googleapis.com/maven2/'
def redirect = { RepositoryHandler repos ->
    repos.withType(MavenArtifactRepository).configureEach { repo ->
        if (central.any { repo.url.toString().startsWith(it) }) {
            repo.url = mirror
        }
    }
}
beforeSettings { settings ->
    redirect(settings.pluginManagement.repositories)
    redirect(settings.dependencyResolutionManagement.repositories)
    redirect(settings.buildscript.repositories)
}
allprojects { project ->
    redirect(project.buildscript.repositories)
    redirect(project.repositories)
}
"""


def say(msg):
    print(f"install-android: {msg}", file=sys.stderr, flush=True)


def run(argv, **kw):
    return subprocess.run(argv, timeout=STEP_TIMEOUT, capture_output=True, text=True, **kw)


def sdkmanager():
    return os.path.join(ANDROID_ROOT, "cmdline-tools", "latest", "bin", "sdkmanager")


def install_sdk():
    if not os.path.exists(sdkmanager()):
        with urllib.request.urlopen(REPO_XML, timeout=60) as resp:
            builds = re.findall(r"commandlinetools-linux-(\d+)_latest\.zip", resp.read().decode())
        if not builds:
            raise RuntimeError("no commandlinetools build listed in " + REPO_XML)
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "tools.zip")
            urllib.request.urlretrieve(TOOLS_ZIP % max(builds, key=int), archive)
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tmp)
            dest = os.path.dirname(os.path.dirname(sdkmanager()))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(os.path.join(tmp, "cmdline-tools"), dest)
        for root, _dirs, files in os.walk(os.path.join(os.path.dirname(sdkmanager()))):
            for name in files:
                os.chmod(os.path.join(root, name), 0o755)
    sdk = [sdkmanager(), f"--sdk_root={ANDROID_ROOT}"]
    run(sdk + ["--licenses"], input="y\n" * 50)
    for pkg in PACKAGES:
        if os.path.isdir(os.path.join(ANDROID_ROOT, *pkg.split(";"))):
            continue
        res = run(sdk + [pkg], input="y\n" * 5)
        if res.returncode != 0:
            say(f"{pkg} failed: {(res.stderr or res.stdout).strip()[-300:]}")
    adb = os.path.join(ANDROID_ROOT, "platform-tools", "adb")
    if os.path.exists(adb):
        link(adb, "adb")


def install_flutter():
    flutter = os.path.join(FLUTTER_ROOT, "bin", "flutter")
    if not os.path.exists(flutter):
        res = run(["git", "clone", "-q", "--depth", "1", "--branch", FLUTTER_VERSION,
                   "https://github.com/flutter/flutter.git", FLUTTER_ROOT])
        if res.returncode != 0:
            raise RuntimeError("flutter clone failed: " + res.stderr.strip()[-300:])
    env = dict(os.environ, CI="true", FLUTTER_SUPPRESS_ANALYTICS="true")
    run([flutter, "config", "--no-analytics", f"--android-sdk={ANDROID_ROOT}"], env=env)
    run([flutter, "--version"], env=env)  # fetches the Dart SDK into the snapshot
    link(flutter, "flutter")
    link(os.path.join(FLUTTER_ROOT, "bin", "dart"), "dart")


def install_maven_mirror():
    os.makedirs(os.path.dirname(GRADLE_INIT), exist_ok=True)
    with open(GRADLE_INIT, "w") as fh:
        fh.write(MAVEN_MIRROR)


def link(target, name):
    if not os.path.isdir(BIN) or not os.access(BIN, os.W_OK):
        return
    path = os.path.join(BIN, name)
    if os.path.lexists(path):
        if os.path.realpath(path) == os.path.realpath(target):
            return
        say(f"{path} exists; leaving it")
        return
    os.symlink(target, path)


def guarded(name, fn, failures):
    try:
        fn()
    except Exception as exc:  # fail open: the hooks matter more than the SDK
        failures.append(name)
        say(f"{name} skipped: {exc}")


def main():
    if os.environ.get("AIORCH_CLOUD_ANDROID") == "0" or not sys.platform.startswith("linux"):
        return 0
    failures = []
    if "--mirror-only" in sys.argv[1:]:  # install-cloud.py --quick, at session start
        guarded("maven-mirror", install_maven_mirror, failures)
        return 0
    threads = [threading.Thread(target=guarded, args=(n, f, failures))
               for n, f in (("android-sdk", install_sdk), ("flutter", install_flutter))]
    for t in threads:
        t.start()
    guarded("maven-mirror", install_maven_mirror, failures)
    for t in threads:
        t.join()
    say("done" + (f" (skipped: {', '.join(failures)})" if failures else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
