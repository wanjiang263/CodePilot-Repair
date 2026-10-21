import { describe, expect, it } from "vitest";

import { isBudgetStop, statusMeta, stopReasonKey, verificationKey } from "./status";

describe("status presentation", () => {
  it("keeps a budget stop out of the failure bucket", () => {
    expect(statusMeta("stopped").labelKey).toBe("status.stopped.label");
    expect(statusMeta("stopped").tone).toBe("warning");
    expect(isBudgetStop("budget_test_runs")).toBe(true);
    expect(isBudgetStop("tests_failed")).toBe(false);
  });

  it("labels internal and official concepts separately", () => {
    expect(verificationKey("m2_verified")).toBe("verification.m2_verified");
    expect(verificationKey("budget_exhausted")).toBe("verification.budget_exhausted");
    expect(stopReasonKey("budget_closure_test_runs")).toBe(
      "stopReasons.budget_closure_test_runs",
    );
  });

  it("does not turn unknown states into success", () => {
    expect(statusMeta("something_new").labelKey).toBe("status.error.label");
    expect(verificationKey(null)).toBe("verification.notRecorded");
    expect(stopReasonKey("something_new")).toBeNull();
  });
});
