// #185 regression fixture: mirrors the real dogeared-coach false positive —
// a file whose path lexically contains only the epic's common entity word
// ("provider") must NOT inherit contracts.json/ui-spec.json operations that
// mention "provider" and nothing more specific. No @cw-trace annotation and
// no artifact-derived binding should govern this file after the CTR-fh-050
// corpus-specificity fix.

export function AuthProvider() {
  return null;
}
