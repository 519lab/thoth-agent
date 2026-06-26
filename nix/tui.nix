# nix/tui.nix — Thoth TUI (Ink/React) compiled with tsc and bundled
{ pkgs, thothNpmLib, ... }:
let
  src = ../ui-tui;
  npmDeps = pkgs.fetchNpmDeps {
    inherit src;
    hash = "sha256-UhR343cgTBMg3ieklzqt90xv0ArFlMHsoxM88GLm50s=";
  };

  npm = thothNpmLib.mkNpmPassthru { folder = "ui-tui"; attr = "tui"; pname = "thoth-tui"; };

  packageJson = builtins.fromJSON (builtins.readFile (src + "/package.json"));
  version = packageJson.version;
in
pkgs.buildNpmPackage (npm // {
  pname = "thoth-tui";
  inherit src npmDeps version;

  doCheck = false;
  npmFlags = [ "--legacy-peer-deps" ];

  installPhase = ''
    runHook preInstall

    mkdir -p $out/lib/thoth-tui

    # Single self-contained bundle built by scripts/build.mjs (esbuild).
    cp -r dist $out/lib/thoth-tui/dist

    # package.json kept for "type": "module" resolution on `node dist/entry.js`.
    cp package.json $out/lib/thoth-tui/

    runHook postInstall
  '';
})
