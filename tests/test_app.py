import os, tempfile, unittest
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import app

class OpportunityHunterTests(unittest.TestCase):
    def setUp(self):
        try: os.remove(os.environ["DATABASE_PATH"])
        except FileNotFoundError: pass
        app.init_db()
    def test_classifies_referral_backend_post(self):
        score, category, summary, skills = app.classify("Java backend engineer — referrals welcome", "We are hiring Spring Boot engineers. Remote in India. DM me", "https://linkedin.com/posts/example")
        self.assertGreaterEqual(score, 40)
        self.assertEqual(category, "Hiring / referral post")
        self.assertIn("Java", skills)
    def test_parses_rss(self):
        xml=b'''<?xml version="1.0"?><rss version="2.0"><channel><item><title>We are hiring Java engineers</title><link>https://www.linkedin.com/posts/example?utm_source=x</link><description>Remote backend roles. Referral welcome.</description><pubDate>Wed, 21 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>'''
        items=app.parse_feed(xml)
        self.assertEqual(len(items),1)
        self.assertIn("linkedin.com/posts/example",items[0]["url"])
        self.assertIn("Remote backend",items[0]["body"])
    def test_deduplicates_url(self):
        item={"title":"Hiring Java engineers","url":"https://www.linkedin.com/posts/test?utm_campaign=x","body":"Java Spring Boot referral"}
        first=app.save_opportunity(item)
        second=app.save_opportunity(item)
        self.assertTrue(first[0]); self.assertFalse(second[0]); self.assertEqual(first[1],second[1])
    def test_rejects_missing_url(self):
        with self.assertRaises(ValueError): app.save_opportunity({"title":"No URL"})

if __name__ == "__main__": unittest.main()
