# nix/web.nix — Thoth Web Dashboard (Vite/React) frontend build
{ pkgs, thothNpmLib, ... }:
let
  src = ../web;
  npmDeps = pkgs.fetchNpmDeps {
    inherit src;
    hash = "sha256-SOGn+lgTJ5CPbUoHe5OWZauDvp/Q6v2UJH7LVvuSsXk=";
  };

  npm = thothNpmLib.mkNpmPassthru { folder = "web"; attr = "web"; pname = "thoth-web"; };

  packageJson = builtins.fromJSON (builtins.readFile (src + "/package.json"));
  version = packageJson.version;
in
pkgs.buildNpmPackage (npm // {
  pname = "thoth-web";
  inherit src npmDeps version;

  doCheck = false;

  buildPhase = ''
    npx tsc -b
    npx vite build --outDir dist
  '';

  installPhase = ''
    runHook preInstall
    cp -r dist $out
    runHook postInstall
  '';
})
