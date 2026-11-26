import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DiscCard, type DiscData } from './DiscCard';
import type { MoveProgress } from '../../types';

function makeDisc(overrides: Partial<DiscData> = {}): DiscData {
  return {
    id: '15',
    title: 'The Matrix',
    subtitle: 'MOVIE • THE_MATRIX',
    discLabel: 'THE_MATRIX',
    coverUrl: '/api/jobs/15/poster',
    mediaType: 'movie',
    state: 'organizing',
    progress: 0,
    tracks: [],
    tracksLoaded: true,
    ...overrides,
  };
}

function makeMove(overrides: Partial<MoveProgress> = {}): MoveProgress {
  return {
    job_id: 15,
    title_index: 1,
    source_name: 'The Matrix_t01.mkv',
    current_bytes: 14_700_000_000,
    total_bytes: 39_531_675_648,
    percent: (14_700_000_000 / 39_531_675_648) * 100,
    speed: '42.1 MB/s',
    eta: 912,
    updated_at: 0,
    ...overrides,
  };
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('DiscCard — verified move progress', () => {
  it('renders the VERIFIED MOVE bar and a decimal-GB readout while bytes are in flight', () => {
    render(<DiscCard disc={makeDisc()} moveProgress={makeMove()} />);

    expect(screen.getByTestId('move-progress')).toBeInTheDocument();
    expect(screen.getByText(/VERIFIED MOVE/)).toBeInTheDocument();
    expect(screen.getByTestId('move-progress-readout')).toHaveTextContent(
      '14.7 GB / 39.5 GB · 42.1 MB/s · 15m 12s left',
    );
  });

  it('omits speed and ETA on the first sample, which carries neither', () => {
    render(
      <DiscCard
        disc={makeDisc()}
        moveProgress={makeMove({ current_bytes: 0, percent: 0, speed: null, eta: null })}
      />,
    );

    expect(screen.getByTestId('move-progress-readout')).toHaveTextContent(/^0\.0 GB \/ 39\.5 GB$/);
  });

  it('shows the verified line once the move is done', () => {
    render(
      <DiscCard
        disc={makeDisc({ state: 'completed' })}
        moveCompleted={makeMove({ current_bytes: 39_531_675_648, percent: 100, speed: null, eta: null })}
      />,
    );

    expect(screen.queryByTestId('move-progress')).not.toBeInTheDocument();
    expect(screen.getByTestId('move-verified')).toHaveTextContent('✓ VERIFIED — 39.5 GB transferred');
  });

  it('renders nothing for a disc with no move', () => {
    render(<DiscCard disc={makeDisc()} />);

    expect(screen.queryByTestId('move-progress')).not.toBeInTheDocument();
    expect(screen.queryByTestId('move-verified')).not.toBeInTheDocument();
  });
});
