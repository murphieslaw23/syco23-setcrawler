import { expect, test } from '@playwright/test'

test('reviews a set on desktop and renders the mobile inbox', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/inbox')

  await expect(page.getByRole('heading', { name: 'Review Inbox' })).toBeVisible()
  await expect(page.locator('.set-row')).toHaveCount(4)
  await page.screenshot({ path: '../../artifacts/inbox-desktop.png', fullPage: true })

  await page.getByLabel('Source').selectOption('soundcloud')
  await expect(page.getByText('K- - B2B ZMK — FREE PARTY SESSION', { exact: true })).toBeVisible()
  await expect(page.getByText('MURPH @ SOUTH SIDE TEKNIVAL 2026', { exact: true })).toBeHidden()
  await page.getByLabel('Source').selectOption('all')

  await page.getByRole('link', { name: 'Review MURPH @ SOUTH SIDE TEKNIVAL 2026' }).click()
  await expect(page.getByRole('heading', { name: 'Field candidates' })).toBeVisible()

  const acceptButtons = page.getByRole('button', { name: 'Accept candidate' })
  await expect(acceptButtons).toHaveCount(5)
  await acceptButtons.first().click()
  await expect(page.getByText('reviewing', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: 'Accept for curation' }).click()
  await expect(page.getByText('accepted', { exact: true })).toBeVisible()
  await page.screenshot({ path: '../../artifacts/review-detail-desktop.png', fullPage: true })

  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/inbox')
  await expect(page.getByRole('navigation', { name: 'Mobile navigation' })).toBeVisible()
  await expect(page.locator('.set-row')).toHaveCount(4)
  await page.screenshot({ path: '../../artifacts/inbox-mobile.png', fullPage: true })
})
