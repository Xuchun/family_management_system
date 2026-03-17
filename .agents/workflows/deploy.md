---
description: Push changes to GitHub and verify Streamlit deployment
---

After any successful code modification and local verification, follow these steps to deploy:

1. Add and commit the changes with a versioned message.
   ```bash
   git add .
   git commit -m "v[VERSION]: [Brief description of changes]"
   ```

2. Push to the main branch.
   // turbo
   3. Run the push command:
   ```bash
   git push origin main
   ```

4. Verify the deployment at:
   https://familymanagementsystem-62a6cbu5jurgnvzngezutj.streamlit.app/?auth_key=authenticated#56c2e491
