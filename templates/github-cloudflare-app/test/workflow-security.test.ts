import { existsSync, readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const readWorkflow = (name: string) =>
  readFileSync(new URL(`../.github/workflows/${name}`, import.meta.url), "utf8");

const readRepositoryFile = (name: string) =>
  readFileSync(new URL(`../${name}`, import.meta.url), "utf8");

describe("preview workflow security boundary", () => {
  it("uses the trusted default-branch workflow with an approval gate", () => {
    const workflow = readWorkflow("pr-preview.yml");

    expect(workflow).toContain("pull_request_target:");
    expect(workflow).toContain("name: Validate PR");
    expect(workflow).toContain("name: Validate Linear ticket branch");
    expect(workflow).toContain("name: Validate preview eligibility");
    expect(workflow).toContain("needs: validate-pr");
    expect(workflow).not.toContain("preview-approval:");
    expect(workflow).toContain("name: Internal / Build preview");
    expect(workflow).toContain("name: Internal / Publish preview");
    expect(workflow).toContain("needs: build-preview");
    expect(workflow).toContain("environment:\n      name: branch-preview");
    expect(workflow).toContain("name: branch-preview");
    expect(workflow).toContain("HEAD_REPO");
    expect(workflow).toContain("BASE_REPO");
    expect(workflow).toContain("ref: ${{ github.event.pull_request.head.sha }}");
    expect(workflow).toContain("persist-credentials: false");
  });

  it("builds without credentials and publishes only after revalidation", () => {
    const workflow = readWorkflow("pr-preview.yml");

    expect(workflow).toContain("name: Internal / Build preview");
    expect(workflow).toContain("actions/upload-artifact@");
    expect(workflow).toContain("name: Internal / Publish preview");
    expect(workflow).toContain("actions/download-artifact@");
    expect(workflow).toContain("Revalidate approved PR revision");
    expect(workflow).toContain("current_sha");
    expect(workflow).toContain("Verify isolated preview Worker has no secrets");
    expect(workflow).toContain("wrangler versions upload");
    expect(workflow).toContain("--preview-alias");
    expect(workflow).toContain("Production deployment was not changed.");
  });

  it("enforces branch and eligibility policy in one visible PR check", () => {
    const workflow = readWorkflow("pr-preview.yml");
    const legacyWorkflow = new URL(
      "../.github/workflows/branch-name.yml",
      import.meta.url,
    );

    expect(existsSync(legacyWorkflow)).toBe(false);
    expect(workflow).toContain("^SIS-[1-9][0-9]*$");
    expect(workflow).toContain(
      "Branch name must be the Linear ticket ID, for example SIS-10.",
    );
    expect(workflow).toContain("gh pr comment");
    expect(workflow).toContain("DRAFT");
    expect(workflow).toContain("HEAD_REPO");
    expect(workflow).toContain("exit 1");
  });

  it("runs CI once per pull request and never on feature-branch push", () => {
    const workflow = readWorkflow("ci.yml");

    expect(workflow).toContain("pull_request:");
    expect(workflow).toContain("branches:\n      - main");
    expect(workflow).not.toContain("push:");
  });

  it("preserves API-first routing in the isolated preview config", () => {
    const config = JSON.parse(
      readRepositoryFile("wrangler.version-preview.jsonc"),
    );

    expect(config.name).toBe("__PREVIEW_WORKER__");
    expect(config.workers_dev).toBe(false);
    expect(config.preview_urls).toBe(true);
    expect(config.assets.binding).toBe("ASSETS");
    expect(config.assets.run_worker_first).toEqual(["/api/*"]);
    expect(config.assets.not_found_handling).toBe("404-page");
  });
});