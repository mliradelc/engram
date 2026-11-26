import '@testing-library/jest-dom';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ReviewQueue from './ReviewQueue';
import type { WebSocketMessage } from '../types';

// Capture the page's WebSocket listener so the test can push move_progress.
const listeners: Array<(msg: WebSocketMessage) => void> = [];
vi.mock('../hooks/useWebSocket', () => ({
    useWebSocket: () => ({
        addMessageListener: (fn: (msg: WebSocketMessage) => void) => {
            listeners.push(fn);
            return () => {
                const i = listeners.indexOf(fn);
                if (i !== -1) listeners.splice(i, 1);
            };
        },
    }),
}));

const TOTAL = 39_531_675_648;

function mockMovieBackend(reviewStatus: number) {
    vi.stubGlobal(
        'fetch',
        vi.fn(async (url: string, init?: RequestInit) => {
            if (url === '/api/jobs/1/review' && init?.method === 'POST') {
                return { ok: false, status: reviewStatus, json: async () => ({}), text: async () => '<html>504 Gateway Time-out</html>' };
            }
            const body = url.endsWith('/titles')
                ? [{
                    id: 7, job_id: 1, title_index: 1, duration_seconds: 8160, file_size_bytes: TOTAL,
                    chapter_count: 30, is_selected: true, output_filename: '/staging/The Matrix_t01.mkv',
                    matched_episode: null, match_confidence: 0, state: 'review',
                }]
                : url.startsWith('/api/config')
                  ? { tmdb_configured: true }
                  : { id: 1, state: 'review_needed', content_type: 'movie', detected_title: 'The Matrix', volume_label: 'THE_MATRIX', tmdb_id: 603 };
            return { ok: true, status: 200, json: async () => body, text: async () => '' };
        }),
    );
}

function push(msg: WebSocketMessage) {
    act(() => {
        for (const fn of [...listeners]) fn(msg);
    });
}

function renderPage() {
    return render(
        <MemoryRouter initialEntries={['/review/1']}>
            <Routes>
                <Route path="/review/:jobId" element={<ReviewQueue />} />
            </Routes>
        </MemoryRouter>,
    );
}

describe('ReviewQueue — a gateway timeout during a verified move is not a failure', () => {
    beforeEach(() => {
        vi.unstubAllGlobals();
        listeners.length = 0;
    });
    afterEach(() => vi.unstubAllGlobals());

    it('shows "still working" and the move bar instead of an error while bytes are arriving', async () => {
        mockMovieBackend(504);
        renderPage();
        const select = await screen.findByRole('button', { name: /^select$/i });

        push({
            type: 'move_progress', job_id: 1, title_index: 1, source_name: 'The Matrix_t01.mkv',
            current_bytes: 14_700_000_000, total_bytes: TOTAL, speed: '42.1 MB/s', eta: 912, done: false,
        });
        expect(await screen.findByText(/VERIFIED MOVE/)).toBeInTheDocument();

        fireEvent.click(select);

        expect(await screen.findByTestId('review-still-working')).toHaveTextContent(
            'Still working — check the progress bar',
        );
        expect(screen.queryByText(/› ERROR:/)).not.toBeInTheDocument();
    });

    it('still reports the 504 as an error when no move was ever seen', async () => {
        mockMovieBackend(504);
        renderPage();
        fireEvent.click(await screen.findByRole('button', { name: /^select$/i }));

        await waitFor(() => expect(screen.getByText(/› ERROR:/)).toBeInTheDocument());
        expect(screen.queryByTestId('review-still-working')).not.toBeInTheDocument();
    });

    it('never masks a real backend error, even mid-move', async () => {
        mockMovieBackend(500);
        renderPage();
        const select = await screen.findByRole('button', { name: /^select$/i });
        push({
            type: 'move_progress', job_id: 1, title_index: 1, source_name: 'The Matrix_t01.mkv',
            current_bytes: 1, total_bytes: TOTAL, speed: null, eta: null, done: false,
        });

        fireEvent.click(select);

        await waitFor(() => expect(screen.getByText(/› ERROR:/)).toBeInTheDocument());
    });
});
