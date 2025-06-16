
"""
*Now we want to read one of our books!* To _read_ one of these books, we will use `Thorium Web` (i.e. our `reader` service). But Thorium Web _only_ knows how to speak in "manifests" which need to be generated: This is the sole job of `readium`.

Once our items are uploaded to, Lenny's `readium` service is preconfigured w/ `s3` so out-of-the-box it can generate manifests for _any_ of its book. To do so, you need to query `readium` to produce a `manifest.json` file for a specified *base64* encoded version of a full s3 filepath:

* e.g.  `<http://localhost:15080/{base64(filepath)}/manifest.json>` where...
    * the book's full s3 filepath is `<s3://bookshelf/32941311.epub>` 
    * its `base64(filepath)` can be computed using:
        * `echo -n "<s3://bookshelf/32941311.epub>" |  base64 | tr '/+' '_-' | tr -d '='` → `czM6Ly9ib29rc2hlbGYvMzI5NDEzMTEuZXB1Yg`
    ◦ resulting in <http://localhost:15080/czM6Ly9ib29rc2hlbGYvMzI5NDEzMTEuZXB1Yg/manifest.json>
Finally, you can navigate to `http:localhost:3000/read?book={readium_manifest_url}` to read the book
* e.g. `<http:localhost:3000/read?book=http://localhost:15080/czM6Ly9ib29rc2hlbGYvMzI5NDEzMTEuZXB1Yg/manifest.json>`
"""

from lenny.core.api import LennyAPI
from lenny.core.utils import encode_book_path
from lenny.configs import READIUM_BASE_URL

class ReadiumAPI:

    @classmethod
    def make_url(cls, book_id, format, readium_path):
        ebp = encode_book_path(book_id, format=format)
        readium_url = f"{READIUM_BASE_URL}/{ebp}/{readium_path}"
        return readium_url

    @classmethod
    def patch_manifest(cls, manifest: dict, book_id: str):
        """Rewrites `self` to link to the correct public url"""
        for i in range(len(manifest['links'])):
            if manifest['links'][i].get('rel') == 'self':
                manifest['links'][i]['href'] = LennyAPI.make_url(
                    f"/v1/api/item/{book_id}/manifest.json"
                )
        return manifest
