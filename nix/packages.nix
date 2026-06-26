# nix/packages.nix — Thoth Agent package built with uv2nix
{ inputs, ... }:
{
  perSystem =
    { pkgs, inputs', ... }:
    let
      thothAgent = pkgs.callPackage ./thoth-agent.nix {
        inherit (inputs) uv2nix pyproject-nix pyproject-build-systems;
        npm-lockfile-fix = inputs'.npm-lockfile-fix.packages.default;
        # Only embed clean revs — dirtyRev doesn't represent any upstream
        # commit, so comparing it would always claim "update available".
        rev = inputs.self.rev or null;
      };
    in
    {
      packages = {
        default = thothAgent;
        tui = thothAgent.thothTui;
        web = thothAgent.thothWeb;

        fix-lockfiles = thothAgent.thothNpmLib.mkFixLockfiles {
          packages = [ thothAgent.thothTui thothAgent.thothWeb ];
        };
      };
    };
}
