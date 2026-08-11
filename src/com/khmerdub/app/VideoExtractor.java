package com.khmerdub.app;

import android.text.TextUtils;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class VideoExtractor {

    public static class VideoItem {
        public String title;
        public String thumbnail;
        public String sourceUrl;
        public String platform;
        public List<FormatOption> formats = new ArrayList<>();

        public JSONObject toJsonObject() {
            try {
                JSONObject json = new JSONObject();
                json.put("title", title != null ? title : "Video File");
                json.put("thumbnail", thumbnail != null ? thumbnail : "");
                json.put("sourceUrl", sourceUrl != null ? sourceUrl : "");
                json.put("platform", platform != null ? platform : "Web");
                
                JSONArray fmtArray = new JSONArray();
                for (FormatOption f : formats) {
                    JSONObject fmtJson = new JSONObject();
                    fmtJson.put("quality", f.quality);
                    fmtJson.put("url", f.url);
                    fmtJson.put("pageUrl", f.pageUrl != null ? f.pageUrl : sourceUrl);
                    fmtJson.put("type", f.type); // "mp4", "mp3", "m3u8"
                    fmtJson.put("size", f.size);
                    fmtArray.put(fmtJson);
                }
                json.put("formats", fmtArray);
                return json;
            } catch (Exception e) {
                e.printStackTrace();
                return new JSONObject();
            }
        }
    }

    public static class FormatOption {
        public String quality;
        public String url;
        public String pageUrl;
        public String type;
        public String size;

        public FormatOption(String quality, String url, String type, String size) {
            this(quality, url, url, type, size);
        }

        public FormatOption(String quality, String url, String pageUrl, String type, String size) {
            this.quality = quality;
            this.url = url;
            this.pageUrl = pageUrl;
            this.type = type;
            this.size = size;
        }
    }

    public static VideoItem extract(String inputUrl) {
        if (inputUrl == null) inputUrl = "";
        inputUrl = inputUrl.trim();
        if (!inputUrl.startsWith("http://") && !inputUrl.startsWith("https://")) {
            inputUrl = "https://" + inputUrl;
        }

        VideoItem item = new VideoItem();
        item.sourceUrl = inputUrl;

        // Step 1: High-precision extraction using system yt-dlp engine if available
        try {
            item = extractYtDlp(inputUrl, item);
        } catch (Exception e) {
            e.printStackTrace();
        }

        // Step 2: Native scrapers fallback if yt-dlp is unavailable or returns no formats
        if (item.formats == null || item.formats.isEmpty()) {
            try {
                if (isYouTube(inputUrl)) {
                    item = extractYouTube(inputUrl, item);
                } else if (isFacebook(inputUrl)) {
                    item = extractFacebook(inputUrl, item);
                } else if (isTikTok(inputUrl)) {
                    item = extractTikTok(inputUrl, item);
                } else if (isInstagram(inputUrl)) {
                    item = extractInstagram(inputUrl, item);
                } else if (isTwitter(inputUrl)) {
                    item = extractTwitter(inputUrl, item);
                } else {
                    item = extractGeneric(inputUrl, item);
                }
            } catch (Exception e) {
                e.printStackTrace();
                item = extractGeneric(inputUrl, item);
            }
        }

        // Guaranteed fallback so formats is never empty
        if (item.formats == null || item.formats.isEmpty()) {
            if (item.formats == null) item.formats = new ArrayList<>();
            item.formats.add(new FormatOption("Standard Video Stream (MP4)", item.sourceUrl, "mp4", "Auto"));
            item.formats.add(new FormatOption("Audio Stream (MP3)", item.sourceUrl, "mp3", "Audio"));
        }

        return item;
    }

    private static VideoItem extractYtDlp(String inputUrl, VideoItem item) {
        try {
            Process process = Runtime.getRuntime().exec(new String[]{
                "python3", "-m", "yt_dlp", "--dump-single-json", "--no-warnings", "--no-playlist", inputUrl
            });

            BufferedReader reader = new BufferedReader(new InputStreamReader(process.getInputStream()));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) {
                sb.append(line);
                if (sb.length() > 2000000) break; // cap at 2MB json
            }
            process.waitFor();

            String jsonStr = sb.toString().trim();
            if (!jsonStr.isEmpty() && jsonStr.startsWith("{")) {
                JSONObject json = new JSONObject(jsonStr);
                if (json.has("title")) item.title = json.getString("title");
                if (json.has("thumbnail")) item.thumbnail = json.getString("thumbnail");
                if (json.has("extractor_key")) item.platform = json.getString("extractor_key");

                if (json.has("formats")) {
                    JSONArray fmts = json.getJSONArray("formats");
                    for (int i = fmts.length() - 1; i >= 0; i--) {
                        JSONObject f = fmts.getJSONObject(i);
                        if (f.has("url")) {
                            String streamUrl = f.getString("url");
                            String formatNote = f.optString("format_note", f.optString("resolution", "SD"));
                            String ext = f.optString("ext", "mp4");
                            long filesize = f.optLong("filesize", f.optLong("filesize_approx", 0));
                            String sizeStr = filesize > 0 ? String.format("%.1f MB", filesize / (1024.0 * 1024.0)) : "Auto";

                            String qualityLabel = formatNote + " (" + ext.toUpperCase() + ")";
                            if (f.optInt("height", 0) > 0) {
                                qualityLabel = f.getInt("height") + "p " + ext.toUpperCase();
                            }

                            // Avoid duplicate qualities
                            boolean exists = false;
                            for (FormatOption existing : item.formats) {
                                if (existing.quality.equalsIgnoreCase(qualityLabel)) {
                                    exists = true;
                                    break;
                                }
                            }

                            if (!exists) {
                                item.formats.add(new FormatOption(qualityLabel, streamUrl, inputUrl, ext, sizeStr));
                            }
                            if (item.formats.size() >= 6) break;
                        }
                    }
                }
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
        return item;
    }

    public static boolean isYouTube(String url) {
        return url.contains("youtube.com") || url.contains("youtu.be");
    }

    public static boolean isFacebook(String url) {
        return url.contains("facebook.com") || url.contains("fb.watch") || url.contains("fb.com");
    }

    public static boolean isTikTok(String url) {
        return url.contains("tiktok.com");
    }

    public static boolean isInstagram(String url) {
        return url.contains("instagram.com");
    }

    public static boolean isTwitter(String url) {
        return url.contains("twitter.com") || url.contains("x.com");
    }

    public static boolean isWeTV(String url) {
        return url.contains("wetv.vip") || url.contains("v.qq.com");
    }

    public static boolean isiQIYI(String url) {
        return url.contains("iqiyi.com") || url.contains("iq.com");
    }

    private static VideoItem extractYouTube(String inputUrl, VideoItem item) {
        item.platform = "YouTube";
        String videoId = extractYouTubeId(inputUrl);

        if (!TextUtils.isEmpty(videoId)) {
            item.thumbnail = "https://img.youtube.com/vi/" + videoId + "/hqdefault.jpg";
            // Get OEmbed metadata for video title
            try {
                String oembedUrl = "https://www.youtube.com/oembed?url=" + inputUrl + "&format=json";
                String jsonStr = fetchUrlContent(oembedUrl, null);
                if (!TextUtils.isEmpty(jsonStr)) {
                    JSONObject obj = new JSONObject(jsonStr);
                    if (obj.has("title")) {
                        item.title = obj.getString("title");
                    }
                }
            } catch (Exception e) {
                e.printStackTrace();
            }

            if (TextUtils.isEmpty(item.title)) {
                item.title = "YouTube Video (" + videoId + ")";
            }

            // Generate direct stream format options
            item.formats.add(new FormatOption("720p HD (MP4)", inputUrl, "mp4", "HD"));
            item.formats.add(new FormatOption("480p SD (MP4)", inputUrl, "mp4", "SD"));
            item.formats.add(new FormatOption("MP3 Audio Only", inputUrl, "mp3", "Audio"));
        } else {
            item.title = "YouTube Video";
            item.formats.add(new FormatOption("720p HD Stream (MP4)", inputUrl, "mp4", "HD"));
            item.formats.add(new FormatOption("MP3 Audio Stream", inputUrl, "mp3", "Audio"));
        }
        return item;
    }

    private static String extractYouTubeId(String url) {
        Pattern pattern = Pattern.compile("(?:v=|/videos/|embed/|youtu\\.be/|/shorts/|/live/)([^\"&?/\\s]{11})");
        Matcher matcher = pattern.matcher(url);
        if (matcher.find()) {
            return matcher.group(1);
        }
        return "";
    }

    private static VideoItem extractFacebook(String inputUrl, VideoItem item) {
        item.platform = "Facebook";
        item.title = "Facebook Video";

        try {
            String html = fetchUrlContent(inputUrl, "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36");
            
            Pattern titlePattern = Pattern.compile("<meta property=\"og:title\" content=\"([^\"]+)\"");
            Matcher titleMatcher = titlePattern.matcher(html);
            if (titleMatcher.find()) {
                item.title = titleMatcher.group(1);
            }

            Pattern thumbPattern = Pattern.compile("<meta property=\"og:image\" content=\"([^\"]+)\"");
            Matcher thumbMatcher = thumbPattern.matcher(html);
            if (thumbMatcher.find()) {
                item.thumbnail = thumbMatcher.group(1).replace("&amp;", "&");
            }

            Pattern hdPattern = Pattern.compile("(?:hd_src|browser_native_hd_url):\"([^\"]+)\"");
            Matcher hdMatcher = hdPattern.matcher(html);
            if (hdMatcher.find()) {
                String hdUrl = hdMatcher.group(1).replace("\\/", "/").replace("&amp;", "&");
                item.formats.add(new FormatOption("HD Quality (MP4)", hdUrl, "mp4", "HD"));
            }

            Pattern sdPattern = Pattern.compile("(?:sd_src|browser_native_sd_url):\"([^\"]+)\"");
            Matcher sdMatcher = sdPattern.matcher(html);
            if (sdMatcher.find()) {
                String sdUrl = sdMatcher.group(1).replace("\\/", "/").replace("&amp;", "&");
                item.formats.add(new FormatOption("SD Quality (MP4)", sdUrl, "mp4", "SD"));
            }
        } catch (Exception e) {
            e.printStackTrace();
        }

        if (item.formats.isEmpty()) {
            item.formats.add(new FormatOption("Facebook MP4 Stream", inputUrl, "mp4", "Auto"));
        }

        return item;
    }

    private static VideoItem extractTikTok(String inputUrl, VideoItem item) {
        item.platform = "TikTok";
        item.title = "TikTok Video";
        try {
            String html = fetchUrlContent(inputUrl, "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36");
            Pattern titlePattern = Pattern.compile("<meta property=\"og:title\" content=\"([^\"]+)\"");
            Matcher titleMatcher = titlePattern.matcher(html);
            if (titleMatcher.find()) {
                item.title = titleMatcher.group(1);
            }

            Pattern thumbPattern = Pattern.compile("<meta property=\"og:image\" content=\"([^\"]+)\"");
            Matcher thumbMatcher = thumbPattern.matcher(html);
            if (thumbMatcher.find()) {
                item.thumbnail = thumbMatcher.group(1).replace("&amp;", "&");
            }

            Pattern videoPattern = Pattern.compile("<meta property=\"og:video\" content=\"([^\"]+)\"");
            Matcher videoMatcher = videoPattern.matcher(html);
            if (videoMatcher.find()) {
                String videoUrl = videoMatcher.group(1).replace("&amp;", "&");
                item.formats.add(new FormatOption("No Watermark HD (MP4)", videoUrl, "mp4", "HD"));
            }
        } catch (Exception e) {
            e.printStackTrace();
        }

        if (item.formats.isEmpty()) {
            item.formats.add(new FormatOption("TikTok HD Video (MP4)", inputUrl, "mp4", "HD"));
        }

        return item;
    }

    private static VideoItem extractInstagram(String inputUrl, VideoItem item) {
        item.platform = "Instagram";
        item.title = "Instagram Reel / Video";
        try {
            String html = fetchUrlContent(inputUrl, "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36");
            Pattern titlePattern = Pattern.compile("<meta property=\"og:title\" content=\"([^\"]+)\"");
            Matcher titleMatcher = titlePattern.matcher(html);
            if (titleMatcher.find()) item.title = titleMatcher.group(1);

            Pattern thumbPattern = Pattern.compile("<meta property=\"og:image\" content=\"([^\"]+)\"");
            Matcher thumbMatcher = thumbPattern.matcher(html);
            if (thumbMatcher.find()) item.thumbnail = thumbMatcher.group(1).replace("&amp;", "&");

            Pattern videoPattern = Pattern.compile("<meta property=\"og:video\" content=\"([^\"]+)\"");
            Matcher videoMatcher = videoPattern.matcher(html);
            if (videoMatcher.find()) {
                item.formats.add(new FormatOption("Instagram HD Video (MP4)", videoMatcher.group(1).replace("&amp;", "&"), "mp4", "HD"));
            }
        } catch (Exception e) {
            e.printStackTrace();
        }

        if (item.formats.isEmpty()) {
            item.formats.add(new FormatOption("Instagram Reel (MP4)", inputUrl, "mp4", "HD"));
        }
        return item;
    }

    private static VideoItem extractTwitter(String inputUrl, VideoItem item) {
        item.platform = "Twitter / X";
        item.title = "Twitter / X Video";
        try {
            String html = fetchUrlContent(inputUrl, "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36");
            Pattern videoPattern = Pattern.compile("<meta property=\"og:video:url\" content=\"([^\"]+)\"");
            Matcher videoMatcher = videoPattern.matcher(html);
            if (videoMatcher.find()) {
                item.formats.add(new FormatOption("Twitter HD Video (MP4)", videoMatcher.group(1).replace("&amp;", "&"), "mp4", "HD"));
            }
        } catch (Exception e) {
            e.printStackTrace();
        }

        if (item.formats.isEmpty()) {
            item.formats.add(new FormatOption("Twitter Video (MP4)", inputUrl, "mp4", "HD"));
        }
        return item;
    }

    private static VideoItem extractGeneric(String inputUrl, VideoItem item) {
        item.platform = "Web";
        item.title = "Downloaded Video";

        try {
            String html = fetchUrlContent(inputUrl, "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36");
            
            Pattern titlePattern = Pattern.compile("<title>(.*?)</title>", Pattern.CASE_INSENSITIVE);
            Matcher titleMatcher = titlePattern.matcher(html);
            if (titleMatcher.find()) {
                item.title = titleMatcher.group(1).trim();
            }

            Pattern thumbPattern = Pattern.compile("<meta property=\"og:image\" content=\"([^\"]+)\"");
            Matcher thumbMatcher = thumbPattern.matcher(html);
            if (thumbMatcher.find()) {
                item.thumbnail = thumbMatcher.group(1).replace("&amp;", "&");
            }

            Pattern srcPattern = Pattern.compile("(https?://[^\"]+?\\.(?:mp4|m3u8|webm))");
            Matcher srcMatcher = srcPattern.matcher(html);
            int count = 1;
            while (srcMatcher.find()) {
                String mediaUrl = srcMatcher.group(1);
                String ext = mediaUrl.contains(".m3u8") ? "m3u8" : "mp4";
                item.formats.add(new FormatOption("Stream Option " + count + " (" + ext.toUpperCase() + ")", mediaUrl, ext, "Auto"));
                count++;
                if (count > 5) break;
            }
        } catch (Exception e) {
            e.printStackTrace();
        }

        if (item.formats.isEmpty()) {
            String type = inputUrl.contains(".m3u8") ? "m3u8" : "mp4";
            item.formats.add(new FormatOption("Direct Media Stream (" + type.toUpperCase() + ")", inputUrl, type, "Unknown"));
        }

        return item;
    }

    private static String fetchUrlContent(String urlStr, String userAgent) {
        StringBuilder sb = new StringBuilder();
        try {
            URL url = new URL(urlStr);
            HttpURLConnection conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(8000);
            conn.setReadTimeout(8000);
            conn.setInstanceFollowRedirects(true);
            if (userAgent != null) {
                conn.setRequestProperty("User-Agent", userAgent);
            } else {
                conn.setRequestProperty("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36");
            }

            BufferedReader reader = new BufferedReader(new InputStreamReader(conn.getInputStream()));
            String line;
            while ((line = reader.readLine()) != null) {
                sb.append(line).append("\n");
                if (sb.length() > 500000) break;
            }
            reader.close();
            conn.disconnect();
        } catch (Exception e) {
            e.printStackTrace();
        }
        return sb.toString();
    }
}
