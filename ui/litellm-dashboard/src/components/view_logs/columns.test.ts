import { describe, expect, it } from "vitest";
import { computeThroughput } from "./columns";

describe("computeThroughput", () => {
  it("returns tokens/sec when both completion_tokens and request_duration_ms are valid", () => {
    expect(computeThroughput(100, 1000)).toBe(100);
    expect(computeThroughput(50, 500)).toBe(100);
  });

  it("returns undefined when completion_tokens is 0 (embedding/error requests)", () => {
    expect(computeThroughput(0, 1000)).toBeUndefined();
  });

  it("returns undefined when request_duration_ms is 0 (div-by-zero guard)", () => {
    expect(computeThroughput(100, 0)).toBeUndefined();
  });

  it("returns undefined when request_duration_ms is negative", () => {
    expect(computeThroughput(100, -100)).toBeUndefined();
  });

  it("returns undefined when either value is undefined or null", () => {
    expect(computeThroughput(undefined, 1000)).toBeUndefined();
    expect(computeThroughput(100, undefined)).toBeUndefined();
    expect(computeThroughput(undefined, undefined)).toBeUndefined();
  });
});
