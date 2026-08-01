import { readFileSync } from 'node:fs'

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import { demoProfiles } from '../../data/demo'

const root = process.cwd()

describe('operator surfaces', () => {
  it('carries every profile run diagnostic in the frontend contract fixture', () => {
    expect(demoProfiles[0]).toMatchObject({
      schedule_timezone: 'Europe/Berlin',
      last_scheduled_at: '2026-07-28T06:00:00Z',
      next_scheduled_at: '2026-07-29T06:00:00Z',
      next_page_token: 'NEXT_PAGE_23',
      last_result_count: 12,
      last_error_code: null,
      latest_job_id: 'd0b00000-0000-4000-8000-000000000003'
    })
  })

  it('renders profile cursor, run result, error, and latest job state', () => {
    const source = readFileSync(`${root}/pages/search-profiles/index.vue`, 'utf8')
    for (const field of [
      'profile.next_page_token',
      'profile.schedule_timezone',
      'profile.last_run_at',
      'profile.last_scheduled_at',
      'profile.next_scheduled_at',
      'profile.last_result_count',
      'profile.last_error_code',
      'profile.latest_job_id'
    ]) {
      expect(source).toContain(field)
    }
  })

  it('wires recent jobs, provider warnings, and direct SoundCloud intake on the dashboard', () => {
    const source = readFileSync(`${root}/pages/index.vue`, 'utf8')
    expect(source).toContain('/imports/queue?limit=5')
    expect(source).toContain('Recent import runs')
    expect(source).toContain('Provider warnings')
    expect(source).toContain('DashboardSoundCloudImport')
  })

  it('mounts the dashboard SoundCloud action with viewer and failure states', async () => {
    const { default: DashboardSoundCloudImport } = await import(
      '../../components/DashboardSoundCloudImport.vue'
    )
    const viewer = mount(DashboardSoundCloudImport, {
      props: {
        canEdit: false,
        busy: false,
        error: '',
        jobId: null
      }
    })
    expect(viewer.get('button').attributes('disabled')).toBeDefined()
    expect(viewer.text()).toContain('Editor access required')

    const failed = mount(DashboardSoundCloudImport, {
      props: {
        canEdit: true,
        busy: false,
        error: 'Broker unavailable',
        jobId: null
      }
    })
    expect(failed.get('[role="alert"]').text()).toContain('Broker unavailable')
  })
})
