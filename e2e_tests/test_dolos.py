# Imports
from playwright.sync_api import Page, expect
from e2e_tests.helpers import login


# Test to check if Dolos is generating the plagiarism report
def test_dolos(page: Page) -> None:
    login(page)
    rows = page.locator('#exercises-table tbody tr').all()
    exercise_row = min(
        (row for row in rows if int(row.locator('td').nth(1).inner_text()) > 1),
        key=lambda row: int(row.locator('td').nth(1).inner_text()),
    )
    exercise_row.locator('td').first.get_by_role('link').click()
    page.get_by_role('link', name='Run analysis').click()
    expect(page.get_by_text('Source code plagiarism')).to_be_visible(timeout=25000)
