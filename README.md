# FirstAIMovers: RSS to Multi-Social with Human-in-the-Loop Approval

## Situation
First AI Movers aims to automate the process of distributing human-approved RSS feed updates to multiple social media platforms (Bluesky, Facebook, and LinkedIn) with AI-powered content optimization. Each platform requires different formatting, character limits, and specific features like Bluesky's facets for clickable links. This project leverages n8n to create a modular workflow that transforms RSS content using Google Gemini and posts platform-optimized versions.

---

## Task
The goal is to build an automated workflow that does the following in order:
1. Monitor an RSS field for new items
2. Use AI/LLM to transform the content of the new item into platform-specific posts
3. Ask for human approval on each platform's post
4. If approved, post to the content to the platform. Otherwise, re-generate the post again and ask for approval
5. Modular design must be adopted for ease of adding new platforms later on 

---

## Action
An n8n workflow was created with the following architecture:

RSS Feed -> Content extraction -> Platform-specific branching -> LLM post generation -> Platform posting module -> Post publishing

---

### Steps:
1. The RSS feed item content is extracted, including the following: 
    - Item title
    - Link
    - Image URL (if present)
    - Paragraphs from item page
2. Extracted content is passed once to each platform-specific branch
    - Facebook
    - Bluesky
    - LinkedIn
3. Platform-specific variables are added to the content:
    - System prompt
    - User prompt
    - Branch name
    - Character limit for platform post
4. Item content + platform-specific variables are passed to the **post creation sub-workflow**, which is responsible for the following:
    - Generating post content for each branch by summarizing the paragraph content in a way that appeals to the platform's audience while sticking to the character limit.
    - Logging the generated content to an Excel file for auditing if needed
5. Human-in-the-loop approval request is sent using a Discord bot to a dedicated Discord channel in a Discord server for the human to approve.
    - If **approved**, we move onto step 6 
    - If **declined**, we go back to step 4 and repeat until human approved.
6. Platform-specific publishing sub-workflow is called

---

## Result
The automated workflow functions correctly:

- Monitors the RSS feed for new items with hourly polling.
- Transforms RSS content using AI to create platform-optimized versions.
- Posts to all three platforms simultaneously with:
    - Bluesky: Clickable links using facets, hashtags, and embedded external content.
    - Facebook: Native posts with uploaded images and longer descriptive content.
    - LinkedIn: Posts with uploaded images and much larger content that caters to LinkedIn audience (2000+ characters).
- Ensures modular architecture for easy addition of future platforms ( Twitter, Mastodon, etc.).
- Maintains consistent branding with #FirstAIMovers hashtag across platforms.

---

## References
- [n8n Documentation](https://docs.n8n.io/)
- [Discord App Documentation](https://discord.com/developers/docs/intro)
- [Facebook Graph API Documentation](https://developers.facebook.com/docs/graph-api/)
- [Bluesky Facets API Documentation](https://docs.bsky.app/docs/advanced-guides/post-richtext)
- [N8n LinkedIn Credentials Documentation](https://docs.n8n.io/integrations/builtin/credentials/linkedin/)

