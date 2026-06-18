import argparse
import re
import requests
import urllib.parse

from pathlib import Path
from typing import List
from packaging import version
from collections import OrderedDict

PRE_GITHUH_RELEASE_URLS = url_pre_github = [
    "https://releases.llvm.org/7.0.1/clang+llvm-7.0.1-x86_64-linux-gnu-ubuntu-18.04.tar.xz",
    "https://releases.llvm.org/7.0.0/clang+llvm-7.0.0-x86_64-linux-gnu-ubuntu-16.04.tar.xz",
    "https://releases.llvm.org/6.0.1/clang+llvm-6.0.1-x86_64-linux-gnu-ubuntu-16.04.tar.xz",
    "https://releases.llvm.org/6.0.0/clang+llvm-6.0.0-x86_64-linux-gnu-ubuntu-16.04.tar.xz",
    "https://releases.llvm.org/5.0.2/clang+llvm-5.0.2-x86_64-linux-gnu-ubuntu-16.04.tar.xz",
    "https://releases.llvm.org/5.0.1/clang+llvm-5.0.1-x86_64-linux-gnu-ubuntu-16.04.tar.xz",
    "https://releases.llvm.org/5.0.0/clang+llvm-5.0.0-linux-x86_64-ubuntu16.04.tar.xz",
    "https://releases.llvm.org/4.0.0/clang+llvm-4.0.0-x86_64-linux-gnu-ubuntu-16.10.tar.xz",
    "https://releases.llvm.org/3.9.1/clang+llvm-3.9.1-x86_64-linux-gnu-ubuntu-16.04.tar.xz",
    "https://releases.llvm.org/3.9.0/clang+llvm-3.9.0-x86_64-linux-gnu-ubuntu-16.04.tar.xz",
    "https://releases.llvm.org/3.8.1/clang+llvm-3.8.1-x86_64-linux-gnu-ubuntu-16.04.tar.xz",
    "https://releases.llvm.org/3.8.0/clang+llvm-3.8.0-x86_64-linux-gnu-ubuntu-16.04.tar.xz",
    "https://releases.llvm.org/3.7.1/clang+llvm-3.7.1-x86_64-linux-gnu-ubuntu-15.10.tar.xz",
    "https://releases.llvm.org/3.7.0/clang+llvm-3.7.0-x86_64-linux-gnu-ubuntu-14.04.tar.xz",
    "https://releases.llvm.org/3.6.2/clang+llvm-3.6.2-x86_64-linux-gnu-ubuntu-15.04.tar.xz",
    "https://releases.llvm.org/3.6.1/clang+llvm-3.6.1-x86_64-linux-gnu-ubuntu-15.04.tar.xz",
    "https://releases.llvm.org/3.6.0/clang+llvm-3.6.0-x86_64-linux-gnu-ubuntu-14.04.tar.xz",
    "https://releases.llvm.org/3.5.2/clang+llvm-3.5.2-x86_64-linux-gnu-ubuntu-14.04.tar.xz",
    "https://releases.llvm.org/3.5.1/clang+llvm-3.5.1-x86_64-linux-gnu.tar.xz",
    "https://releases.llvm.org/3.5.0/clang+llvm-3.5.0-x86_64-linux-gnu-ubuntu-14.04.tar.xz",
    "https://releases.llvm.org/3.4.2/clang+llvm-3.4.2-x86_64-linux-gnu-ubuntu-14.04.xz",
    "https://releases.llvm.org/3.4.1/clang+llvm-3.4.1-x86_64-unknown-ubuntu12.04.tar.xz",
    "https://releases.llvm.org/3.4/clang+llvm-3.4-x86_64-linux-gnu-ubuntu-13.10.tar.xz",
    "https://releases.llvm.org/3.3/clang+llvm-3.3-Ubuntu-13.04-x86_64-linux-gnu.tar.bz2",
    "https://releases.llvm.org/3.2/clang+llvm-3.2-x86_64-linux-ubuntu-12.04.tar.gz",
    "https://releases.llvm.org/3.1/clang+llvm-3.1-x86_64-linux-ubuntu_12.04.tar.gz",
    "https://releases.llvm.org/3.0/clang+llvm-3.0-x86_64-linux-Ubuntu-11_10.tar.gz",
    "https://releases.llvm.org/2.9/clang+llvm-2.9-x86_64-linux.tar.bz2",
    "https://releases.llvm.org/2.8/clang+llvm-2.8-x86_64-linux.tar.bz2",
    "https://releases.llvm.org/2.7/clang+llvm-2.7-x86_64-linux.tar.bz2",
    "https://releases.llvm.org/2.6/llvm+clang-2.6-x86_64-linux.tar.gz"
]

def get_clang_llvm_releases(after_version: str) -> List[str]:
    result : List[str] = []

    page = 0
    ubuntu_release_pattern = r"^clang\+llvm-(\d+\.\d+\.\d+)-x86_64-linux-gnu-ubuntu-(\d+\.\d+)\.tar\.xz$"
    while True:
        response = requests.get(f'https://api.github.com/repos/llvm/llvm-project/releases?page={page}')
        if len(response.json()) == 0:
            break
        if response.status_code != 200:
            raise Exception(response.json()['message']) 

        for release in response.json():
            # Skip Pre-release version
            if release['prerelease']:
                continue
            
            # Get release for latest ubuntu version
            latest_ubuntu_release = ""
            latest_ubuntu_version = ""
            for asset in release['assets']:
                url = urllib.parse.unquote(asset['browser_download_url'])
                tar_file = url.split('/')[-1]
                match = re.match(ubuntu_release_pattern, tar_file)
                if not match:
                    continue

                # Version smaller than requested versions, return result
                if version.parse(match.group(1)) <= version.parse(after_version):
                    return list(reversed(OrderedDict.fromkeys(result)))
                
                if latest_ubuntu_version == "" or version.parse(match.group(2)) > version.parse(latest_ubuntu_version):
                    latest_ubuntu_version = match.group(2)
                    latest_ubuntu_release = url
            if latest_ubuntu_release != "":
                result.append(latest_ubuntu_release)
        page += 1

    # Continue searching for releases from `PRE_GITHUH_RELEASE_URLS`
    for release_url in PRE_GITHUH_RELEASE_URLS:
        release_version = release_url.replace("https://releases.llvm.org/", '').split('/')[0]

        if version.parse(release_version) <= version.parse(after_version):
            break 
        result.append(release_url)

    return list(reversed(OrderedDict.fromkeys(result)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("after_version", help="Get list of llvm release after this version", type=str)
    args = parser.parse_args()
    release_urls = get_clang_llvm_releases(args.after_version)
    for url in release_urls:
        print(url)