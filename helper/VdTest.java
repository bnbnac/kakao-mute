import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * shell 권한(app_process)에서 더미 Surface(ImageReader)를 붙인 가상 디스플레이를 만들고
 * 그 위에 앱을 실행한다. 화면에는 아무것도 표시되지 않는다.
 *
 * 인자(key=value): name size dpi app hold drain trusted flags
 *   drain=0 : Surface 소비자가 프레임을 안 비우는 경우(앱 렌더링이 멈추는지 확인용)
 *   trusted=0 : TRUSTED 계열 플래그 제외
 *   flags=0x... : 플래그 값을 직접 지정
 *
 * hold 는 안전 상한이다. 표준입력으로 개행("quit") 또는 EOF 가 들어오면 즉시 해제하고 종료한다.
 */
public class VdTest {
    private static final String SHELL_PACKAGE = "com.android.shell";
    private static final int SHELL_UID = 2000;

    // scrcpy 소스 기억 기반의 값. 틀리면 flags= 로 덮어쓰거나 이 상수를 고친다.
    private static final int FLAG_PUBLIC = 1 << 0;
    private static final int FLAG_OWN_CONTENT_ONLY = 1 << 3;
    private static final int FLAG_SUPPORTS_TOUCH = 1 << 6;
    private static final int FLAG_ROTATES_WITH_CONTENT = 1 << 7;
    private static final int FLAG_DESTROY_CONTENT_ON_REMOVAL = 1 << 8;
    private static final int FLAG_SHOULD_SHOW_SYSTEM_DECORATIONS = 1 << 9;
    private static final int FLAG_TRUSTED = 1 << 10;
    private static final int FLAG_OWN_DISPLAY_GROUP = 1 << 11;
    private static final int FLAG_ALWAYS_UNLOCKED = 1 << 12;
    private static final int FLAG_TOUCH_FEEDBACK_DISABLED = 1 << 13;
    private static final int FLAG_OWN_FOCUS = 1 << 14;

    public static void main(String[] argv) throws Exception {
        Map<String, String> a = parse(argv);
        String name = a.getOrDefault("name", "kmute-vd");
        String[] size = a.getOrDefault("size", "1080x2340").split("x");
        int width = Integer.parseInt(size[0]);
        int height = Integer.parseInt(size[1]);
        int dpi = Integer.parseInt(a.getOrDefault("dpi", "420"));
        String app = a.getOrDefault("app", "com.kakao.talk/.activity.SplashActivity");
        int hold = Integer.parseInt(a.getOrDefault("hold", "60"));
        boolean drain = !"0".equals(a.getOrDefault("drain", "1"));
        boolean trusted = !"0".equals(a.getOrDefault("trusted", "1"));

        int flags = FLAG_PUBLIC | FLAG_OWN_CONTENT_ONLY | FLAG_SUPPORTS_TOUCH
                | FLAG_ROTATES_WITH_CONTENT | FLAG_DESTROY_CONTENT_ON_REMOVAL
                | FLAG_SHOULD_SHOW_SYSTEM_DECORATIONS;
        if (trusted && Build.VERSION.SDK_INT >= 33) {
            flags |= FLAG_TRUSTED | FLAG_OWN_DISPLAY_GROUP | FLAG_ALWAYS_UNLOCKED | FLAG_TOUCH_FEEDBACK_DISABLED;
        }
        if (trusted && Build.VERSION.SDK_INT >= 34) {
            flags |= FLAG_OWN_FOCUS;
        }
        if (a.containsKey("flags")) {
            flags = Integer.decode(a.get("flags"));
        }

        Looper.prepareMainLooper();
        Context ctx = new FakeContext(systemContext());

        ImageReader reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2);
        AtomicInteger frames = new AtomicInteger();
        HandlerThread drainThread = new HandlerThread("kmute-drain");
        drainThread.start();
        if (drain) {
            reader.setOnImageAvailableListener(r -> {
                Image img = r.acquireLatestImage();
                if (img != null) {
                    img.close();
                    frames.incrementAndGet();
                }
            }, new Handler(drainThread.getLooper()));
        }

        Constructor<DisplayManager> ctor = DisplayManager.class.getDeclaredConstructor(Context.class);
        ctor.setAccessible(true);
        DisplayManager dm = ctor.newInstance(ctx);
        VirtualDisplay vd = dm.createVirtualDisplay(name, width, height, dpi, reader.getSurface(), flags);
        if (vd == null) {
            System.out.println("FAIL createVirtualDisplay returned null");
            System.exit(1);
        }
        int displayId = vd.getDisplay().getDisplayId();
        System.out.println("DISPLAY_ID=" + displayId + " name=" + name + " flags=0x" + Integer.toHexString(flags)
                + " sdk=" + Build.VERSION.SDK_INT + " drain=" + drain);

        if (!"none".equals(app)) {
            String cmd = "am start --display " + displayId + " -n " + app;
            System.out.println("RUN " + cmd);
            Process p = new ProcessBuilder("sh", "-c", cmd).redirectErrorStream(true).start();
            try (BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream()))) {
                String line;
                while ((line = r.readLine()) != null) System.out.println("  " + line);
            }
            p.waitFor();
        }

        AtomicBoolean quit = new AtomicBoolean();
        Thread stdinWatcher = new Thread(() -> {
            try {
                int c;
                while ((c = System.in.read()) != -1 && c != '\n') {
                }
            } catch (Exception ignored) {
            }
            quit.set(true);
        });
        stdinWatcher.setDaemon(true);
        stdinWatcher.start();

        for (int i = 1; i <= hold && !quit.get(); i++) {
            Thread.sleep(1000);
            if (i % 2 == 0) System.out.println("t=" + i + "s frames=" + frames.get());
        }
        vd.release();
        drainThread.quit();
        System.out.println("RELEASED");
        System.exit(0);
    }

    private static Map<String, String> parse(String[] argv) {
        Map<String, String> m = new HashMap<>();
        for (String s : argv) {
            int i = s.indexOf('=');
            if (i > 0) m.put(s.substring(0, i), s.substring(i + 1));
        }
        return m;
    }

    private static Context systemContext() throws Exception {
        Class<?> at = Class.forName("android.app.ActivityThread");
        Constructor<?> ctor = at.getDeclaredConstructor();
        ctor.setAccessible(true);
        Object thread = ctor.newInstance();
        Field cur = at.getDeclaredField("sCurrentActivityThread");
        cur.setAccessible(true);
        cur.set(null, thread);
        Field sys = at.getDeclaredField("mSystemThread");
        sys.setAccessible(true);
        sys.setBoolean(thread, true);
        try {
            Class<?> cc = Class.forName("android.app.ConfigurationController");
            Constructor<?> cctor = cc.getDeclaredConstructors()[0];
            cctor.setAccessible(true);
            Field mcc = at.getDeclaredField("mConfigurationController");
            mcc.setAccessible(true);
            mcc.set(thread, cctor.newInstance(thread));
        } catch (ClassNotFoundException | NoSuchFieldException ignored) {
        }
        Method m = at.getDeclaredMethod("getSystemContext");
        return (Context) m.invoke(thread);
    }

    private static class FakeContext extends ContextWrapper {
        FakeContext(Context base) {
            super(base);
        }

        @Override
        public String getPackageName() {
            return SHELL_PACKAGE;
        }

        @Override
        public String getOpPackageName() {
            return SHELL_PACKAGE;
        }

        @Override
        public Context getApplicationContext() {
            return this;
        }

        @Override
        public AttributionSource getAttributionSource() {
            try {
                Class<?> b = Class.forName("android.content.AttributionSource$Builder");
                Object builder = b.getConstructor(int.class).newInstance(SHELL_UID);
                b.getMethod("setPackageName", String.class).invoke(builder, SHELL_PACKAGE);
                return (AttributionSource) b.getMethod("build").invoke(builder);
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        }
    }
}
