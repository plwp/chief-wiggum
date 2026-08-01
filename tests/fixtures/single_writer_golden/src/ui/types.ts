// A DTO interface mentioning "plan" as a field name — must NOT be flagged as
// a writer (no assignment, no mutation context, just a type declaration).
export interface ProviderDTO {
  plan: string;
  stripe_plan: string;
}
