// Generated from schema/plan.schema.json. Do not edit by hand.

/**
 * Repo-relative path
 */
export type Path = string;
/**
 * Why this file is impacted
 */
export type Reason = string;
export type ImpactedFiles = ImpactedFile[];
export type OpenQuestions = string[];
/**
 * A plan must propose at least one concrete change
 *
 * @minItems 1
 */
export type ProposedChanges = [ProposedChange, ...ProposedChange[]];
export type ChangeAction = "create" | "modify" | "delete";
/**
 * What to change and why, specific enough to implement
 */
export type Description = string;
/**
 * Repo-relative path (may be a new file)
 */
export type File = string;
export type Risks = string[];
export type SchemaVersion = 1;
/**
 * One-paragraph plan summary
 */
export type Summary = string;
export type TestStrategy = string;
export type TicketType = "feature" | "bug" | "refactor" | "chore" | "unknown";

export interface Plan {
  impacted_files: ImpactedFiles;
  open_questions: OpenQuestions;
  proposed_changes: ProposedChanges;
  risks: Risks;
  schema_version?: SchemaVersion;
  summary: Summary;
  test_strategy: TestStrategy;
  ticket_type: TicketType;
}
export interface ImpactedFile {
  path: Path;
  reason: Reason;
}
export interface ProposedChange {
  action: ChangeAction;
  description: Description;
  file: File;
}
