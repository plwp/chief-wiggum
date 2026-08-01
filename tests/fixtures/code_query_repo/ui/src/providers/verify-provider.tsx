// #185 regression fixture: the positive counterpart to auth-provider.tsx —
// this file's path contains BOTH the common entity word ("provider") AND a
// specific verb word ("verify") that also appears in the "Verify Provider"
// operation path (/api/v1/providers/:id/verify). The entity+verb combination
// must still bind even though "providers" alone is corpus-common.

export function VerifyProvider() {
  return null;
}
