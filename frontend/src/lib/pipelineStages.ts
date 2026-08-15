/**
 * Pure grouping for the pipeline board: cards in, ordered stages out.
 *
 * Kept out of the component for the same reason `browse.ts` is — the rules
 * that are easy to get subtly wrong (which status belongs where, what an
 * unknown status does, what an empty stage looks like) are testable here
 * without mounting anything. Change the stage vocabulary in this file, never
 * at a call site.
 */

import type { PipelineCard, PipelineStatus } from './types';

export interface PipelineStage {
  /** The status this stage collects, and its stable key for `{#each}`. */
  key: PipelineStatus;
  /** What a friend reads. Plain language, never an upstream status word. */
  label: string;
  cards: PipelineCard[];
}

/**
 * Stages in board order, top first.
 *
 * Ordered by how much is happening rather than by position in the journey:
 * the reason to open this page is "is my thing moving", so transfers lead and
 * finished work sinks. `available` last also means the section that grows
 * without bound (a fortnight of completed requests) never pushes the active
 * ones off the first screen.
 */
const STAGE_ORDER: readonly { key: PipelineStatus; label: string }[] = [
  { key: 'downloading', label: 'Downloading' },
  { key: 'processing', label: 'Finding a copy' },
  { key: 'partially_available', label: 'Partly there' },
  { key: 'requested', label: 'Awaiting approval' },
  { key: 'available', label: 'Ready to watch' },
] as const;

/**
 * Where a status with no stage of its own goes.
 *
 * `unknown` is what the backend emits for a Jellyseerr status code this build
 * predates. Dropping such a card would make a real request silently invisible,
 * so it joins the stage that already means "upstream is working on it".
 */
const FALLBACK_STAGE: PipelineStatus = 'processing';

/**
 * Group cards into the stages that have any, in board order.
 *
 * Empty stages are omitted rather than rendered as headers over nothing: with
 * both arr queues idle — the normal state — a board that always drew every
 * stage would be mostly empty labels.
 *
 * @param cards Cards as served by `GET /api/pipeline`, in upstream order.
 * @returns One entry per non-empty stage; card order within a stage is
 *   whatever the board arrived in.
 */
export const groupByStage = (cards: PipelineCard[]): PipelineStage[] => {
  const known = new Set(STAGE_ORDER.map((s) => s.key));

  return STAGE_ORDER.map(({ key, label }) => ({
    key,
    label,
    cards: cards.filter((card) =>
      known.has(card.status) ? card.status === key : key === FALLBACK_STAGE,
    ),
  })).filter((stage) => stage.cards.length > 0);
};
