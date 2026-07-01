# Building & distributing the `.deb`

The agent ships as a native Debian package that depends on stock apt packages
(`python3-paramiko`, `python3-pyte`, `python3-flask`, `python3-requests`,
`python3-waitress`, `arp-scan`) — so there is **no pip/venv step** at install
time and it works offline.

## Build the package

Do this on a Debian/Raspberry Pi OS machine (or an `arm64`/`amd64` container).
Architecture is `all`, so a build on any Debian works for every Pi.

```bash
sudo apt-get install -y build-essential debhelper devscripts
cd hipac-shp-agent
dpkg-buildpackage -us -uc -b        # unsigned binary build
# result: ../hipac-shp-agent_0.1.0_all.deb
```

Bump the version by adding a new top entry to `debian/changelog`
(`dch -v 0.2.0` if you have devscripts).

## Install / test a build

```bash
sudo apt-get install ./hipac-shp-agent_0.1.0_all.deb   # pulls deps automatically
sudo systemctl status hipac-agent
```

Then place the receiver key and configure via the web UI (see main README).

## Hosting an apt repository (so `apt-get install hipac-shp-agent` works)

Lightweight option — a flat repo published on GitHub Pages / any static host:

```bash
# one-time: make a signing key, export the public key for clients
gpg --quick-gen-key "HiPAC APT <ops@eastec.com.au>"

# per release, in a repo dir containing the .deb(s):
mkdir -p apt/pool apt/dists/stable/main/binary-all
cp *.deb apt/pool/
cd apt
dpkg-scanpackages pool /dev/null > dists/stable/main/binary-all/Packages
gzip -k -f dists/stable/main/binary-all/Packages
apt-ftparchive release dists/stable > dists/stable/Release
gpg --clearsign -o dists/stable/InRelease dists/stable/Release
gpg -abs -o dists/stable/Release.gpg dists/stable/Release
# publish the apt/ folder to a static host
```

Client (each Pi), one-time:

```bash
curl -fsSL https://apt.eastec.com.au/pubkey.gpg | sudo tee /usr/share/keyrings/hipac.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/hipac.gpg] https://apt.eastec.com.au stable main" \
  | sudo tee /etc/apt/sources.list.d/hipac.list
sudo apt-get update
sudo apt-get install hipac-shp-agent
```

For a fully managed alternative, push the `.deb` to a Cloudsmith/packagecloud
repo and point the Pis at that instead.
