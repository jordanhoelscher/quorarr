import { describe, expect, it } from 'vitest';

import { groupByStage } from './pipelineStages';
import type { PipelineCard, PipelineStatus } from './types';

const card = (status: PipelineStatus, title: string = status): PipelineCard => ({
  title,
  media_type: 'movie',
  tmdb_id: null,
  poster: null,
  requested_by: 'Sam',
  created_at: '2026-08-15T12:00:00+00:00',
  status,
  pct: null,
  timeleft: null,
  warning: null,
  count: null,
});

describe('groupByStage', () => {
  it('puts each status under its own stage', () => {
    const stages = groupByStage([
      card('downloading'),
      card('processing'),
      card('partially_available'),
      card('requested'),
      card('available'),
    ]);

    expect(stages.map((s) => [s.key, s.cards.length])).toEqual([
      ['downloading', 1],
      ['processing', 1],
      ['partially_available', 1],
      ['requested', 1],
      ['available', 1],
    ]);
  });

  it('orders stages by what is moving, not by journey position', () => {
    // Given deliberately backwards input, the board still leads with transfers.
    const stages = groupByStage([card('available'), card('requested'), card('downloading')]);

    expect(stages.map((s) => s.key)).toEqual(['downloading', 'requested', 'available']);
  });

  it('omits a stage nothing is in', () => {
    // The everyday case: both arr queues empty, so there is no "Downloading".
    const stages = groupByStage([card('processing'), card('available')]);

    expect(stages.map((s) => s.key)).toEqual(['processing', 'available']);
  });

  it('folds an unrecognised status into "finding a copy"', () => {
    // A jellyseerr status code this build predates must still show the title,
    // not vanish from the board.
    const stages = groupByStage([card('unknown')]);

    expect(stages).toHaveLength(1);
    expect(stages[0].key).toBe('processing');
    expect(stages[0].cards[0].title).toBe('unknown');
  });

  it('gives every stage a human label', () => {
    const stages = groupByStage([card('downloading'), card('partially_available')]);

    expect(stages.map((s) => s.label)).toEqual(['Downloading', 'Partly there']);
  });

  it('keeps upstream order within a stage', () => {
    const stages = groupByStage([
      card('processing', 'first'),
      card('processing', 'second'),
      card('processing', 'third'),
    ]);

    expect(stages[0].cards.map((c) => c.title)).toEqual(['first', 'second', 'third']);
  });

  it('returns nothing for an empty board', () => {
    expect(groupByStage([])).toEqual([]);
  });
});
